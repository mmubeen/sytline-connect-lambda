"""T005: the mock takes its settings, limits, routes and permissions from src/template.yaml (specs/008 R4, FR-008)."""

import pytest

import render_env


@pytest.fixture(scope="module")
def r():
    return render_env.render()


def test_function_name_and_limits_come_from_the_template(r):
    assert r["function_name"] == "sytline-connect-fn"
    assert r["timeout"] == 300
    assert r["memory"] == 256
    assert r["handler"] == "lambda_function.lambda_handler"


def test_environment_has_every_name_the_template_declares(r, template):
    declared = set(template["Globals"]["Function"]["Environment"]["Variables"]) | set(
        template["Resources"]["DirectFunction"]["Properties"]["Environment"]["Variables"])
    assert declared <= set(r["env"])


def test_environment_values_are_resolved_not_raw(r):
    env = r["env"]
    assert env["JOBS_TABLE"] == "sytline-connect-JobsTable"
    assert env["SYTELINE_LIVE_WRITES"] == "false"
    assert env["SYTELINE_SQL_ENCRYPT"] == "true"
    assert env["IDP_STACK_NAME"] == "mock-idp"
    assert env["SYTELINE_SECRET_ARN"].startswith("arn:aws-us-gov:secretsmanager:")
    assert not any(str(v).startswith("@") or "${" in str(v) for v in env.values())


def test_parameters_override_defaults():
    r = render_env.render(params={"SqlEncrypt": "false", "MatchCap": "5"})
    assert r["env"]["SYTELINE_SQL_ENCRYPT"] == "false"
    assert r["env"]["SYTELINE_MATCH_CAP"] == "5"


def test_unknown_parameter_is_refused():
    with pytest.raises(KeyError):
        render_env.render(params={"NoSuchParameter": "x"})


def test_routes_come_from_the_api_definition(r):
    assert ("POST", "/confirmed-orders") in r["routes"]
    assert ("GET", "/upload-jobs/{id}") in r["routes"]
    assert ("POST", "/job-reservations/{id}/save") in r["routes"]
    assert len(r["routes"]) == 5


def test_no_api_without_an_endpoint():
    assert render_env.render(params={"ApiVpcEndpointId": ""})["routes"] == []


def _actions(r):
    return {a for s in r["statements"] for a in s["Action"]}


def test_statements_are_expanded_with_resolved_resources(r):
    secret = [s for s in r["statements"] if "secretsmanager:GetSecretValue" in s["Action"]]
    assert secret[0]["Resource"] == [r["env"]["SYTELINE_SECRET_ARN"]]
    invoke = [s for s in r["statements"] if "lambda:InvokeFunction" in s["Action"]]
    resources = {x for s in invoke for x in s["Resource"]}
    assert "arn:aws-us-gov:lambda:us-gov-east-1:000000000000:function:sytline-connect-fn" in resources
    assert "arn:aws-us-gov:lambda:us-gov-east-1:000000000000:function:mock-idp-LookupFunction-*" in resources


def test_dynamodb_crud_policy_template_is_expanded(r):
    ddb = [s for s in r["statements"] if "dynamodb:PutItem" in s["Action"]]
    assert ddb and "arn:aws-us-gov:dynamodb:us-gov-east-1:000000000000:table/sytline-connect-JobsTable" in ddb[0]["Resource"]
    assert "dynamodb:DeleteItem" in ddb[0]["Action"]


def test_kms_statement_only_with_keys():
    assert "kms:Decrypt" in _actions(render_env.render())
    assert "kms:Decrypt" not in _actions(render_env.render(params={"IdpKmsKeyArns": ""}))


def test_idp_statements_only_when_configured():
    r = render_env.render(params={"IdpStackName": "", "IdpOutputBucketName": "", "IdpTrackingTableName": ""})
    assert "s3:GetObject" not in _actions(r)
    assert "cloudformation:DescribeStacks" not in _actions(r)


def test_nothing_else_is_granted(r):
    # the function role must not gain anything beyond what the template states (guards policy.py tests)
    assert "s3:PutObject" not in _actions(r)
    assert "dynamodb:DeleteTable" not in _actions(r)


def test_schedule_input(r):
    assert r["schedule"] == {"expression": "rate(5 minutes)", "input": {"action": "sweep"}}


def test_a_template_with_an_unsupported_intrinsic_fails_loudly(template):
    import copy

    t = copy.deepcopy(template)
    t["Resources"]["DirectFunction"]["Properties"]["Environment"]["Variables"]["X"] = {"Fn::ImportValue": "y"}
    with pytest.raises(render_env.Unsupported):
        render_env.render(template=t)


def test_real_account_region_and_identifiers_reach_the_permission_resources():
    r = render_env.render(account="123456789012", region="us-gov-west-1", partition="aws-us-gov",
                          params={"IdpStackName": "us-ct-idp-stack", "IdpOutputBucketName": "real-out",
                                  "IdpTrackingTableName": "real-track",
                                  "IdpKmsKeyArns": "arn:aws-us-gov:kms:us-gov-west-1:123456789012:key/abc-1"})
    resources = {x for s in r["statements"] for x in s["Resource"]}
    assert "arn:aws-us-gov:lambda:us-gov-west-1:123456789012:function:us-ct-idp-stack-LookupFunction-*" in resources
    assert "arn:aws-us-gov:s3:::real-out/*" in resources
    assert "arn:aws-us-gov:dynamodb:us-gov-west-1:123456789012:table/real-track" in resources
    assert "arn:aws-us-gov:kms:us-gov-west-1:123456789012:key/abc-1" in resources
    assert r["function_arn"] == "arn:aws-us-gov:lambda:us-gov-west-1:123456789012:function:sytline-connect-fn"
    assert "123456789012" in r["role_arn"]
    assert "000000000000" not in str(r["statements"])


def test_defaults_are_unchanged_by_the_new_arguments():
    assert render_env.render() == render_env.render(account="000000000000", region="us-gov-east-1", partition="aws-us-gov")
