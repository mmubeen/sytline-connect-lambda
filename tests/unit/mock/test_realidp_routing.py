"""T049: which requests are IDP-bound, and the read-only allow-list (FR-019)."""

import pytest

from edge import actions, realidp

P, R, A = "aws-us-gov", "us-gov-east-1", "123456789012"
CFG = realidp.RealConfig(stack="us-ct-idp-stack", bucket="idp-output-bkt", table="idp-tracking", region=R, partition=P)
AUTH = f"AWS4-HMAC-SHA256 Credential=K/20260101/{R}/%s/aws4_request, SignedHeaders=host, Signature=00"


def needs(service, method="POST", path="/", target=None, body=b""):
    req = actions.parse_request(method, path, {"Authorization": AUTH % service, **({"X-Amz-Target": target} if target else {})},
                                {}, body)
    return req, actions.identify_all(req, P, R, A)


def bound(*a, **k):
    req, n = needs(*a, **k)
    return CFG.is_idp_bound(req, n)


def test_the_idp_stack_is_bound():
    assert bound("cloudformation", body=b"Action=DescribeStacks&StackName=us-ct-idp-stack")
    assert bound("cloudformation", body=b"Action=ListStackResources&StackName=us-ct-idp-stack")


def test_another_stack_is_not():
    assert not bound("cloudformation", body=b"Action=DescribeStacks&StackName=some-other-stack")


def test_the_output_bucket_and_table_are_bound():
    assert bound("s3", method="GET", path="/idp-output-bkt/doc.pdf/sections/1/result.json")
    assert not bound("s3", method="GET", path="/another-bucket/x")
    assert bound("dynamodb", target="DynamoDB_20120810.GetItem", body=b'{"TableName":"idp-tracking","Key":{}}')
    assert not bound("dynamodb", target="DynamoDB_20120810.GetItem", body=b'{"TableName":"sytline-connect-JobsTable","Key":{}}')


def test_only_the_lookup_function_is_bound():
    assert bound("lambda", path="/2015-03-31/functions/us-ct-idp-stack-LookupFunction-VrNVNbYjvQb6/invocations")
    assert not bound("lambda", path="/2015-03-31/functions/sytline-connect-fn/invocations")


def test_secrets_and_the_jobs_table_stay_mocked():
    assert not bound("secretsmanager", target="secretsmanager.GetSecretValue", body=b'{"SecretId":"x"}')
    assert not bound("dynamodb", target="DynamoDB_20120810.PutItem", body=b'{"TableName":"sytline-connect-JobsTable"}')


@pytest.mark.parametrize("service,kw,allowed", [
    ("cloudformation", dict(body=b"Action=DescribeStacks&StackName=us-ct-idp-stack"), True),
    ("cloudformation", dict(body=b"Action=DeleteStack&StackName=us-ct-idp-stack"), False),
    ("cloudformation", dict(body=b"Action=UpdateStack&StackName=us-ct-idp-stack"), False),
    ("s3", dict(method="GET", path="/idp-output-bkt/k"), True),
    ("s3", dict(method="PUT", path="/idp-output-bkt/k"), False),
    ("s3", dict(method="DELETE", path="/idp-output-bkt/k"), False),
    ("dynamodb", dict(target="DynamoDB_20120810.Query", body=b'{"TableName":"idp-tracking"}'), True),
    ("dynamodb", dict(target="DynamoDB_20120810.BatchGetItem", body=b'{"RequestItems":{"idp-tracking":{}}}'), True),
    ("dynamodb", dict(target="DynamoDB_20120810.PutItem", body=b'{"TableName":"idp-tracking"}'), False),
    ("dynamodb", dict(target="DynamoDB_20120810.DeleteItem", body=b'{"TableName":"idp-tracking"}'), False),
    ("lambda", dict(path="/2015-03-31/functions/us-ct-idp-stack-LookupFunction-X/invocations"), True),
])
def test_read_only_allow_list(service, kw, allowed):
    _, n = needs(service, **kw)
    assert realidp.read_only(n) is allowed
    # BatchGetItem's resource comes from RequestItems; the others from the request
