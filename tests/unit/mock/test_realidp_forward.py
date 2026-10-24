"""T051: the edge re-signs IDP requests with the developer's credentials and sends them on."""

import threading
import time

import pytest
from werkzeug.serving import make_server
from werkzeug.wrappers import Request, Response

from edge import realidp

KEY, SECRET, TOKEN = "ASIADEVELOPERKEY", "dev-secret-value", "dev-session-token-value"


@pytest.fixture
def real():
    seen = []

    @Request.application
    def app(request):
        seen.append({"method": request.method, "path": request.full_path.rstrip("?"), "headers": dict(request.headers),
                     "body": request.get_data()})
        return Response(b'{"ok": true}', 200, {"Content-Type": "application/json", "X-Real": "1"})

    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.seen = seen
    srv.url = f"http://127.0.0.1:{srv.server_port}"
    yield srv
    srv.shutdown()


@pytest.fixture
def forward(real, tmp_path):
    path = tmp_path / "creds"
    path.write_text(f"AWS_ACCESS_KEY_ID={KEY}\nAWS_SECRET_ACCESS_KEY={SECRET}\nAWS_SESSION_TOKEN={TOKEN}\n"
                    f"AWS_CREDENTIAL_EXPIRATION={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 3600))}\n")
    path.chmod(0o600)
    cfg = realidp.RealConfig(stack="s", bucket="b", table="t", region="us-gov-east-1", partition="aws-us-gov",
                             endpoint_for=lambda service: real.url)
    return realidp.RealForward(cfg, realidp.Credentials(str(path)))


FAKE = {"Authorization": "AWS4-HMAC-SHA256 Credential=MOCKFAKEKEY/20260101/us-gov-east-1/dynamodb/aws4_request, SignedHeaders=host, Signature=00",
        "X-Amz-Date": "20260101T000000Z", "X-Amz-Security-Token": "fake", "X-Amz-Target": "DynamoDB_20120810.GetItem",
        "Content-Type": "application/x-amz-json-1.0", "User-Agent": "Boto3/1.0", "X-Amz-Content-Sha256": "fake"}


def test_the_request_is_signed_with_the_developers_credentials_not_the_functions(forward, real):
    status, headers, body = forward("POST", "/", FAKE, {}, b'{"TableName":"t"}', "dynamodb")
    seen = real.seen[0]
    auth = seen["headers"]["Authorization"]
    assert f"Credential={KEY}/" in auth and "MOCKFAKEKEY" not in auth
    assert "/us-gov-east-1/dynamodb/aws4_request" in auth
    assert seen["headers"]["X-Amz-Security-Token"] == TOKEN
    assert status == 200 and body == b'{"ok": true}' and headers["X-Real"] == "1"


def test_body_target_and_content_type_are_kept(forward, real):
    forward("POST", "/", FAKE, {}, b'{"TableName":"t"}', "dynamodb")
    seen = real.seen[0]
    assert seen["body"] == b'{"TableName":"t"}' and seen["headers"]["X-Amz-Target"] == "DynamoDB_20120810.GetItem"
    assert seen["headers"]["Content-Type"] == "application/x-amz-json-1.0"


def test_the_functions_own_headers_are_not_passed_on(forward, real):
    forward("POST", "/", FAKE, {}, b"{}", "dynamodb")
    headers = real.seen[0]["headers"]
    assert headers["X-Amz-Date"] != "20260101T000000Z"          # re-dated by the signer
    assert "fake" not in headers.get("X-Amz-Content-Sha256", "")


def test_s3_uses_its_signer_and_keeps_the_path(forward, real):
    forward("GET", "/b/doc.pdf/sections/1/result.json", {**FAKE, "X-Amz-Target": ""}, {}, b"", "s3")
    seen = real.seen[0]
    assert seen["path"] == "/b/doc.pdf/sections/1/result.json"
    assert "/s3/aws4_request" in seen["headers"]["Authorization"] and "X-Amz-Content-Sha256" in seen["headers"]


def test_lambda_invocation_type_is_kept(forward, real):
    forward("POST", "/2015-03-31/functions/s-LookupFunction-X/invocations", {**FAKE, "X-Amz-Target": "",
            "X-Amz-Invocation-Type": "RequestResponse"}, {}, b"{}", "lambda")
    assert real.seen[0]["headers"]["X-Amz-Invocation-Type"] == "RequestResponse"


def test_expired_credentials_are_not_used(tmp_path, real):
    path = tmp_path / "c"
    path.write_text(f"AWS_ACCESS_KEY_ID={KEY}\nAWS_SECRET_ACCESS_KEY={SECRET}\nAWS_SESSION_TOKEN={TOKEN}\n"
                    "AWS_CREDENTIAL_EXPIRATION=2020-01-01T00:00:00Z\n")
    path.chmod(0o600)
    cfg = realidp.RealConfig(stack="s", bucket="b", table="t", region="us-gov-east-1", partition="aws-us-gov",
                             endpoint_for=lambda s: real.url)
    with pytest.raises(realidp.CredentialsUnavailable):
        realidp.RealForward(cfg, realidp.Credentials(str(path)))("POST", "/", FAKE, {}, b"{}", "dynamodb")
    assert real.seen == []


def test_default_endpoints():
    gov = realidp.RealConfig(stack="s", bucket="b", table="t", region="us-gov-east-1", partition="aws-us-gov")
    assert gov.endpoint("cloudformation") == "https://cloudformation-fips.us-gov-east-1.amazonaws.com"
    assert gov.endpoint("s3") == "https://s3-fips.us-gov-east-1.amazonaws.com"
    nofips = realidp.RealConfig(stack="s", bucket="b", table="t", region="us-gov-east-1", partition="aws-us-gov", fips=False)
    assert nofips.endpoint("lambda") == "https://lambda.us-gov-east-1.amazonaws.com"
    com = realidp.RealConfig(stack="s", bucket="b", table="t", region="us-east-1", partition="aws")
    assert com.endpoint("dynamodb") == "https://dynamodb.us-east-1.amazonaws.com"
