"""T009: map an AWS request to the IAM action and resource it needs, and read the request apart."""

from edge import actions

P, R, A = "aws-us-gov", "us-gov-east-1", "000000000000"
AUTH = f"AWS4-HMAC-SHA256 Credential=AKIAMOCK/20260101/{R}/%s/aws4_request, SignedHeaders=host, Signature=00"


def req(service, method="POST", path="/", headers=None, query=None, body=b""):
    h = {"Authorization": AUTH % service, **(headers or {})}
    return actions.parse_request(method, path, h, query or {}, body)


def test_dynamodb_put_item():
    r = req("dynamodb", headers={"X-Amz-Target": "DynamoDB_20120810.PutItem"}, body=b'{"TableName":"jobs","Item":{}}')
    assert (r["service"], r["operation"]) == ("dynamodb", "PutItem")
    assert actions.identify(r, P, R, A) == ("dynamodb:PutItem", f"arn:{P}:dynamodb:{R}:{A}:table/jobs")


def test_secrets_manager_get_secret_value_by_arn():
    arn = f"arn:{P}:secretsmanager:{R}:{A}:secret:mock/x-AbCdEf"
    r = req("secretsmanager", headers={"X-Amz-Target": "secretsmanager.GetSecretValue"},
            body=('{"SecretId":"%s"}' % arn).encode())
    assert actions.identify(r, P, R, A) == ("secretsmanager:GetSecretValue", arn)


def test_s3_get_object_path_style():
    r = req("s3", method="GET", path="/bucket-1/dir/file.json")
    assert r["operation"] == "GetObject"
    assert actions.identify(r, P, R, A) == ("s3:GetObject", f"arn:{P}:s3:::bucket-1/dir/file.json")


def test_s3_put_object():
    r = req("s3", method="PUT", path="/bucket-1/k")
    assert actions.identify(r, P, R, A)[0] == "s3:PutObject"


def test_cloudformation_describe_stacks_uses_the_query_action():
    r = req("cloudformation", body=b"Action=DescribeStacks&StackName=mock-idp&Version=2010-05-15")
    assert r["operation"] == "DescribeStacks" and r["params"]["StackName"] == "mock-idp"
    action, resource = actions.identify(r, P, R, A)
    assert action == "cloudformation:DescribeStacks"
    assert resource.startswith(f"arn:{P}:cloudformation:{R}:{A}:stack/mock-idp/")


def test_lambda_invoke_by_name_and_by_arn():
    r = req("lambda", path="/2015-03-31/functions/mock-idp-LookupFunction-ABC/invocations")
    assert r["operation"] == "Invoke"
    assert actions.identify(r, P, R, A) == ("lambda:InvokeFunction", f"arn:{P}:lambda:{R}:{A}:function:mock-idp-LookupFunction-ABC")
    full = f"arn:{P}:lambda:{R}:{A}:function:other"
    r = req("lambda", path=f"/2015-03-31/functions/{full}/invocations")
    assert actions.identify(r, P, R, A)[1] == full


def test_service_is_read_from_the_signed_scope():
    assert req("kms", headers={"X-Amz-Target": "TrentService.Decrypt"}, body=b"{}")["service"] == "kms"


def test_unknown_operation_is_unmapped_and_will_be_denied():
    r = req("dynamodb", headers={"X-Amz-Target": "DynamoDB_20120810.DeleteTable"}, body=b'{"TableName":"jobs"}')
    # still mapped: DeleteTable is a real action, so the policy (not the table) decides
    assert actions.identify(r, P, R, A)[0] == "dynamodb:DeleteTable"
    odd = req("somethingnew", path="/x")
    assert actions.identify(odd, P, R, A)[0].startswith("unmapped:")


def test_unsigned_request_has_no_service():
    r = actions.parse_request("GET", "/", {}, {}, b"")
    assert r["service"] is None


def test_a_transaction_needs_the_action_of_each_item_not_an_action_of_its_own():
    body = (b'{"TransactItems":[{"Put":{"TableName":"jobs","Item":{}}},{"Update":{"TableName":"jobs","Key":{}}},'
            b'{"Delete":{"TableName":"other","Key":{}}},{"ConditionCheck":{"TableName":"jobs","Key":{}}}]}')
    r = req("dynamodb", headers={"X-Amz-Target": "DynamoDB_20120810.TransactWriteItems"}, body=body)
    table = lambda n: f"arn:{P}:dynamodb:{R}:{A}:table/{n}"  # noqa: E731
    assert actions.identify_all(r, P, R, A) == [("dynamodb:PutItem", table("jobs")), ("dynamodb:UpdateItem", table("jobs")),
                                                 ("dynamodb:DeleteItem", table("other")),
                                                 ("dynamodb:ConditionCheckItem", table("jobs"))]


def test_batch_write_needs_put_and_delete_on_each_table():
    body = b'{"RequestItems":{"jobs":[{"PutRequest":{"Item":{}}},{"DeleteRequest":{"Key":{}}}]}}'
    r = req("dynamodb", headers={"X-Amz-Target": "DynamoDB_20120810.BatchWriteItem"}, body=body)
    assert [a for a, _ in actions.identify_all(r, P, R, A)] == ["dynamodb:PutItem", "dynamodb:DeleteItem"]


def test_a_plain_request_needs_one_action():
    r = req("s3", method="GET", path="/b/k")
    assert actions.identify_all(r, P, R, A) == [actions.identify(r, P, R, A)]
