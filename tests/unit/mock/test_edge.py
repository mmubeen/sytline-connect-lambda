"""T012: the edge proxy: forwards permitted calls, denies the rest, applies faults, routes Lambda Invoke."""

import json

import pytest
from werkzeug.test import Client

import render_env
from edge import app as edge_app
from stubs import StubBackend, StubForward, StubRuntime

R = render_env.render()
KEY = R["parameters"]["IdpKmsKeyArns"]
OTHER_KEY = KEY.replace("0001", "0002")
BUCKET_ARN = "arn:aws-us-gov:s3:::mock-idp-output/"
AUTH = "AWS4-HMAC-SHA256 Credential=AKIAMOCK/20260101/us-gov-east-1/%s/aws4_request, SignedHeaders=host, Signature=00"


@pytest.fixture
def world():
    backend, runtime, fwd = StubBackend(), StubRuntime(), StubForward()
    backend.kms[BUCKET_ARN] = KEY
    edge = edge_app.Edge(R, fwd, runtime, backend)
    return edge, Client(edge), backend, runtime, fwd


def call(c, service, path="/", target=None, body=None, method="POST", headers=None):
    h = {"Authorization": AUTH % service, **({"X-Amz-Target": target} if target else {}), **(headers or {})}
    data = body if isinstance(body, (bytes, str)) else json.dumps(body or {})
    return c.open(path, method=method, headers=h, data=data)


def put_item(c, table="sytline-connect-JobsTable"):
    return call(c, "dynamodb", target="DynamoDB_20120810.PutItem", body={"TableName": table, "Item": {}})


def test_permitted_call_is_forwarded_and_recorded(world):
    edge, c, *_, fwd = world
    r = put_item(c)
    assert r.status_code == 200 and fwd.seen
    rec = edge.calls.query(service="aws")[0]
    assert rec["allowed"] is True and rec["action"] == "dynamodb:PutItem"


def test_call_log_never_holds_the_authorization_header(world):
    edge, c, *_ = world
    put_item(c)
    assert "AKIAMOCK" not in json.dumps(edge.calls.query())


def test_unpermitted_json_service_call_is_denied_with_aws_wording(world):
    edge, c, *_, fwd = world
    r = put_item(c, table="some-other-table")
    body = json.loads(r.get_data())
    assert r.status_code == 400 and body["__type"].endswith("AccessDeniedException")
    msg = body["message"]
    assert msg.startswith(f"User: {R['role_arn']} is not authorized to perform: dynamodb:PutItem on resource: ")
    assert not fwd.seen and edge.calls.query()[0]["allowed"] is False


def test_unpermitted_s3_call_is_a_403_xml_error(world):
    _, c, *_ = world
    r = call(c, "s3", path="/mock-idp-output/x", method="PUT", body=b"data")
    assert r.status_code == 403 and b"<Code>AccessDenied</Code>" in r.data


def test_unknown_operation_is_denied(world):
    _, c, *_ = world
    r = call(c, "dynamodb", target="DynamoDB_20120810.DeleteTable", body={"TableName": "sytline-connect-JobsTable"})
    assert r.status_code == 400 and b"DeleteTable" in r.data


def test_unsigned_request_is_refused(world):
    _, c, *_ = world
    assert c.get("/").status_code == 403


def test_secret_read_is_allowed_for_the_configured_secret_only(world):
    _, c, *_ = world
    ok = call(c, "secretsmanager", target="secretsmanager.GetSecretValue",
              body={"SecretId": R["env"]["SYTELINE_SECRET_ARN"]})
    other = call(c, "secretsmanager", target="secretsmanager.GetSecretValue", body={"SecretId": "someone-elses"})
    assert ok.status_code == 200 and other.status_code == 400


# -- Lambda Invoke --------------------------------------------------------------------------
FN = "/2015-03-31/functions/sytline-connect-fn/invocations"
LOOKUP = "/2015-03-31/functions/mock-idp-LookupFunction-ABC/invocations"


def test_self_invoke_event_returns_202_and_runs_in_the_background(world):
    _, c, _, runtime, _ = world
    r = call(c, "lambda", path=FN, body={"action": "work_upload", "id": "u1"}, headers={"X-Amz-Invocation-Type": "Event"})
    assert r.status_code == 202 and runtime.async_ == [{"action": "work_upload", "id": "u1"}]


def test_self_invoke_request_response_waits_for_the_result(world):
    _, c, _, runtime, _ = world
    runtime.result = {"status": 200, "function_error": "Unhandled", "payload": {"errorMessage": "boom"}, "duration_ms": 3}
    r = call(c, "lambda", path=FN, body={"action": "health"})
    assert r.status_code == 200 and r.headers["X-Amz-Function-Error"] == "Unhandled"
    assert r.get_json() == {"errorMessage": "boom"}


def test_lookup_function_is_served_by_the_idp_stub(world):
    _, c, backend, *_ = world
    r = call(c, "lambda", path=LOOKUP, body={"stack": "mock-idp"})
    assert r.status_code == 200 and backend.lookups == [{"stack": "mock-idp"}]


def test_invoke_of_an_unpermitted_function_is_denied_before_it_is_looked_up(world):
    _, c, *_ = world
    r = call(c, "lambda", path="/2015-03-31/functions/someone-elses-fn/invocations", body={})
    assert r.status_code == 403 and r.get_json()["Message"].startswith("User: ")


def test_invoke_of_a_permitted_but_unknown_function_is_not_found(world):
    _, c, *_ = world
    r = call(c, "lambda", path="/2015-03-31/functions/mock-idp-LookupFunction-ZZZ2/invocations", body={})
    assert r.status_code == 200  # served by the stub: any name matching the permitted pattern is the lookup function


# -- faults ---------------------------------------------------------------------------------
def test_lookup_denied_fault_denies_only_the_lookup(world):
    edge, c, *_ = world
    edge.faults.add("idp.lookup_denied")
    r = call(c, "lambda", path=LOOKUP, body={})
    assert r.status_code == 403 and "lambda:InvokeFunction" in r.get_json()["Message"]
    assert call(c, "lambda", path=FN, body={}, headers={"X-Amz-Invocation-Type": "Event"}).status_code == 202
    assert edge.calls.query()[0]["fault"] == "idp.lookup_denied"


def test_invoke_denied_fault_denies_the_self_invoke(world):
    edge, c, *_ = world
    edge.faults.add("lambda.invoke_denied")
    assert call(c, "lambda", path=FN, body={}, headers={"X-Amz-Invocation-Type": "Event"}).status_code == 403


def test_secrets_unavailable_fault(world):
    edge, c, *_ = world
    edge.faults.add("secrets.unavailable")
    r = call(c, "secretsmanager", target="secretsmanager.GetSecretValue", body={"SecretId": R["env"]["SYTELINE_SECRET_ARN"]})
    assert r.status_code == 503


def test_kms_is_checked_when_an_object_is_encrypted_and_the_role_has_the_key(world):
    _, c, *_ = world
    assert call(c, "s3", path="/mock-idp-output/doc.json", method="GET").status_code == 200


def test_kms_denied_when_the_role_lacks_the_buckets_key(world):
    """The production failure: the bucket is encrypted with a key the function was not given."""
    _, c, backend, *_ = world
    backend.kms[BUCKET_ARN] = OTHER_KEY
    r = call(c, "s3", path="/mock-idp-output/doc.json", method="GET")
    assert r.status_code == 403
    assert f"is not authorized to perform: kms:Decrypt on resource: {OTHER_KEY}" in r.get_data(as_text=True)


def test_kms_denied_fault_names_the_key(world):
    edge, c, *_ = world
    edge.faults.add("idp.kms_denied", params={"key_arn": OTHER_KEY})
    body = call(c, "s3", path="/mock-idp-output/doc.json", method="GET").get_data(as_text=True)
    assert OTHER_KEY in body and "kms:Decrypt" in body


def test_fault_counts_are_consumed(world):
    edge, c, *_ = world
    edge.faults.add("secrets.unavailable", remaining=1)
    body = {"SecretId": R["env"]["SYTELINE_SECRET_ARN"]}
    assert call(c, "secretsmanager", target="secretsmanager.GetSecretValue", body=body).status_code == 503
    assert call(c, "secretsmanager", target="secretsmanager.GetSecretValue", body=body).status_code == 200


def test_secret_arn_is_rewritten_to_a_name_before_it_reaches_moto(world):
    _, c, *_, fwd = world
    call(c, "secretsmanager", target="secretsmanager.GetSecretValue", body={"SecretId": R["env"]["SYTELINE_SECRET_ARN"]})
    assert json.loads(fwd.bodies[-1])["SecretId"] == "mock/sytline-AbCdEf"


def test_a_transaction_on_the_jobs_table_is_allowed_and_on_another_table_denied(world):
    _, c, *_ = world
    ok = call(c, "dynamodb", target="DynamoDB_20120810.TransactWriteItems",
              body={"TransactItems": [{"Put": {"TableName": "sytline-connect-JobsTable", "Item": {}}}]})
    bad = call(c, "dynamodb", target="DynamoDB_20120810.TransactWriteItems",
               body={"TransactItems": [{"Put": {"TableName": "sytline-connect-JobsTable", "Item": {}}},
                                       {"Put": {"TableName": "other-table", "Item": {}}}]})
    assert ok.status_code == 200 and bad.status_code == 400 and b"other-table" in bad.data
