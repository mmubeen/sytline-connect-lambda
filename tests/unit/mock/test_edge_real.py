"""T053: the edge in real-IDP mode: IDP reads go out (re-signed), everything else stays mocked, writes never leave."""

import json
import logging
import threading
import time

import pytest
from werkzeug.serving import make_server
from werkzeug.test import Client
from werkzeug.wrappers import Request, Response

import render_env
from edge import app as edge_app
from edge import realidp
from stubs import StubBackend, StubForward, StubRuntime

R = render_env.render()
P = R["parameters"]
KEY, SECRET, TOKEN = "ASIADEVELOPERKEY", "dev-secret-value-xyz", "dev-session-token-xyz"
AUTH = "AWS4-HMAC-SHA256 Credential=MOCKFAKEKEY/20260101/us-gov-east-1/%s/aws4_request, SignedHeaders=host, Signature=00"


@pytest.fixture
def real_server():
    seen = []

    @Request.application
    def app(request):
        seen.append(request.full_path)
        return Response(json.dumps({"from": "real"}), 200, {"Content-Type": "application/json"})

    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.seen = seen
    srv.url = f"http://127.0.0.1:{srv.server_port}"
    yield srv
    srv.shutdown()


def creds_file(tmp_path, expires):
    f = tmp_path / "creds"
    f.write_text(f"AWS_ACCESS_KEY_ID={KEY}\nAWS_SECRET_ACCESS_KEY={SECRET}\nAWS_SESSION_TOKEN={TOKEN}\n"
                 f"AWS_CREDENTIAL_EXPIRATION={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(expires))}\n")
    f.chmod(0o600)
    return str(f)


@pytest.fixture
def world(real_server, tmp_path):
    cfg = realidp.RealConfig(P["IdpStackName"], P["IdpOutputBucketName"], P["IdpTrackingTableName"], "us-gov-east-1",
                             "aws-us-gov", endpoint_for=lambda s: real_server.url)
    creds = realidp.Credentials(creds_file(tmp_path, time.time() + 3600))
    backend, mock_fwd = StubBackend(), StubForward()
    edge = edge_app.Edge(R, mock_fwd, StubRuntime(), backend, real=(cfg, realidp.RealForward(cfg, creds), creds))
    return edge, Client(edge), real_server, mock_fwd, tmp_path


def call(c, service, path="/", target=None, body=None, method="POST"):
    h = {"Authorization": AUTH % service, **({"X-Amz-Target": target} if target else {})}
    return c.open(path, method=method, headers=h, data=body if isinstance(body, (bytes, str)) else json.dumps(body or {}))


def test_an_idp_read_goes_to_the_real_service(world):
    edge, c, real, mock, _ = world
    r = call(c, "s3", path=f"/{P['IdpOutputBucketName']}/doc.pdf/sections/1/result.json", method="GET")
    assert r.status_code == 200 and json.loads(r.data) == {"from": "real"}
    assert real.seen and not mock.seen
    rec = edge.calls.query(service="aws")[0]
    assert rec["target"] == "real" and rec["allowed"] is True


def test_the_stack_lookup_goes_to_the_real_service(world):
    edge, c, real, mock, _ = world
    r = call(c, "cloudformation", body=f"Action=DescribeStacks&StackName={P['IdpStackName']}")
    assert r.status_code == 200 and real.seen and not mock.seen


def test_other_calls_stay_on_the_mock(world):
    edge, c, real, mock, _ = world
    call(c, "dynamodb", target="DynamoDB_20120810.PutItem", body={"TableName": "sytline-connect-JobsTable", "Item": {}})
    call(c, "secretsmanager", target="secretsmanager.GetSecretValue", body={"SecretId": R["env"]["SYTELINE_SECRET_ARN"]})
    assert mock.seen and not real.seen
    assert {r["target"] for r in edge.calls.query(service="aws")} == {"mock"}


def test_a_write_to_the_idp_is_refused_and_never_sent(world):
    edge, c, real, mock, _ = world
    # the template would not allow it either; make the role allow it to prove the real-mode rule is separate
    edge.r = {**edge.r, "statements": edge.r["statements"] + [{"Effect": "Allow", "Action": ["s3:PutObject"], "Resource": ["*"]}]}
    r = call(c, "s3", path=f"/{P['IdpOutputBucketName']}/x", method="PUT", body=b"data")
    assert r.status_code == 403 and b"read-only" in r.data
    assert not real.seen and not mock.seen
    rec = edge.calls.query()[-1]
    assert rec["target"] == "real" and rec["allowed"] is False


def test_the_templates_permissions_still_apply_before_routing(world):
    edge, c, real, *_ = world
    r = call(c, "dynamodb", target="DynamoDB_20120810.Scan", body={"TableName": P["IdpTrackingTableName"]})
    assert r.status_code == 400 and not real.seen   # Scan is not granted by the template


def test_expired_credentials_answer_expired_token_and_nothing_falls_back(world, tmp_path):
    edge, c, real, mock, tmp = world
    creds_file(tmp, time.time() - 60)
    r = call(c, "dynamodb", target="DynamoDB_20120810.GetItem", body={"TableName": P["IdpTrackingTableName"], "Key": {}})
    assert r.status_code == 400 and b"ExpiredToken" in r.data and b"expired at" in r.data
    assert not real.seen and not mock.seen
    rec = edge.calls.query()[-1]
    assert rec["allowed"] is False and rec["target"] == "real"


def test_no_mock_kms_check_is_made_for_a_real_read(world):
    edge, c, real, mock, _ = world
    edge.backend.kms[f"arn:aws-us-gov:s3:::{P['IdpOutputBucketName']}/"] = P["IdpKmsKeyArns"].replace("0001", "0002")
    r = call(c, "s3", path=f"/{P['IdpOutputBucketName']}/k", method="GET")
    assert r.status_code == 200      # the real service decrypts (or refuses) itself


def test_no_secret_value_reaches_a_record_a_response_or_a_log(world, caplog):
    edge, c, real, mock, tmp = world
    caplog.set_level(logging.DEBUG)
    out = [call(c, "s3", path=f"/{P['IdpOutputBucketName']}/k", method="GET").get_data(as_text=True)]
    creds_file(tmp, time.time() - 60)
    out.append(call(c, "s3", path=f"/{P['IdpOutputBucketName']}/k", method="GET").get_data(as_text=True))
    text = json.dumps(edge.calls.query(), default=str) + "".join(out) + caplog.text
    for secret in (KEY, SECRET, TOKEN):
        assert secret not in text


def test_without_real_mode_nothing_is_ever_sent_out(real_server):
    edge = edge_app.Edge(R, StubForward(), StubRuntime(), StubBackend())
    c = Client(edge)
    call(c, "s3", path=f"/{P['IdpOutputBucketName']}/k", method="GET")
    assert not real_server.seen
    assert edge.calls.query(service="aws")[0]["target"] == "mock"
