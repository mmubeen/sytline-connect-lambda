"""The single-function deployment shape of src/template.yaml (specs/007-single-function-existing-vpc, SC-001, SC-002)."""

import os
import re

import pytest

from cfn_eval import Evaluator

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")
NET = dict(WorkerVpcId="vpc-0abc", WorkerSubnetIds="subnet-a,subnet-b", WorkerSecurityGroupIds="sg-1",
           ApiVpcEndpointId="vpce-1")
ARN = "arn:aws-us-gov:states:us-gov-east-1:108375521309:stateMachine:idp-DocumentProcessingWorkflow"
ROUTE_VARS = {"SYTELINE_NEXT_CO_IDO": "NextCoIdo", "SYTELINE_NEXT_JOB_IDO": "NextJobIdo",
              "SYTELINE_CUSTOMER_IDO": "CustomerIdo", "SYTELINE_DEFAULTS_IDO": "DefaultsIdo"}
TUNING_VARS = {"SYTELINE_SITE_ID": "SiteId", "SYTELINE_MATCH_MIN_SCORE": "MatchMinScore",
               "SYTELINE_MATCH_MARGIN": "MatchMargin", "SYTELINE_MATCH_CAP": "MatchCap",
               "SYTELINE_SQL_VERIFY_HOSTNAME": "SqlVerifyHostname", "SYTELINE_SQL_LOGIN_TIMEOUT": "SqlLoginTimeout",
               "SYTELINE_SQL_TIMEOUT": "SqlTimeout"}
CODE_DEFAULTS = {"SiteId": "10", "MatchMinScore": "70", "MatchMargin": "3", "MatchCap": "1000",
                 "SqlVerifyHostname": "true", "SqlLoginTimeout": "5", "SqlTimeout": "10"}
REMOVED_PARAMETERS = ("WorkerRouteTableIds", "HostNetworkCidr", "PlantCidr1", "PlantCidr2", "PlantCidr3",
                      "DynamoDbPrefixListId", "CreateVpcEndpoints", "CreateApiEndpoint", "SytelineWsdlHttps",
                      "SytelineWsdlHttp", "AllowPlainHttp", "VerifyTls", "AttachVpcId", "AttachSubnetIds",
                      "AttachSecurityGroupIds", "DatabaseCheck")


def fn(template):
    return template["Resources"]["DirectFunction"]["Properties"]


def env(template):
    return fn(template)["Environment"]["Variables"]


def of_type(template, prefix):
    return {n: r for n, r in template["Resources"].items() if r["Type"].startswith(prefix)}


# ---- US1: one function, nothing network-shaped is created -------------------------------------------------------------
def test_the_stack_creates_exactly_one_function_and_no_network_resource(template):
    assert list(of_type(template, "AWS::Serverless::Function")) == ["DirectFunction"]
    assert of_type(template, "AWS::Lambda::Function") == {}
    assert of_type(template, "AWS::EC2::") == {}  # no VPC, subnet, route table, gateway, endpoint, group or rule
    types = {r["Type"] for r in template["Resources"].values()}
    assert types == {"AWS::Serverless::LayerVersion", "AWS::DynamoDB::Table", "AWS::Serverless::Api",
                     "AWS::Serverless::Function", "AWS::Events::Rule", "AWS::Lambda::Permission",
                     "AWS::S3::Bucket", "AWS::S3::BucketPolicy"}


def test_the_network_is_supplied_not_created_and_is_required(template):
    params = template["Parameters"]
    for name, good, bad in (("WorkerVpcId", "vpc-0abc123", "subnet-1"),
                            ("WorkerSubnetIds", "subnet-a1,subnet-b2", "subnet-a1, subnet-b2"),
                            ("WorkerSecurityGroupIds", "sg-1,sg-2", "sg-1;rm")):
        assert "Default" not in params[name], f"{name} must be required (no default)"
        pattern = re.compile(params[name]["AllowedPattern"])
        assert pattern.match(good) and not pattern.match(bad) and not pattern.match("")


def test_the_api_endpoint_is_optional_and_no_endpoint_means_no_api(template):
    param = template["Parameters"]["ApiVpcEndpointId"]
    assert param["Default"] == ""
    pattern = re.compile(param["AllowedPattern"])
    assert pattern.match("") and pattern.match("vpce-0abc") and not pattern.match("vpc-1") and not pattern.match("vpce-")
    assert Evaluator(template, **{**NET, "ApiVpcEndpointId": ""}).condition("HasApi") is False
    assert Evaluator(template, **NET).condition("HasApi") is True
    for name in ("OrderApi", "OrderApiPermission"):
        assert template["Resources"][name]["Condition"] == "HasApi", name
    assert template["Outputs"]["ApiId"]["Condition"] == "HasApi"
    text = repr(template["Resources"]["DirectFunction"])
    assert "OrderApi" not in text  # the function never refers to the API, so it exists with or without it


def test_the_function_is_attached_to_the_supplied_subnets_and_groups(template):
    e = Evaluator(template, **NET)
    vpc = fn(template)["VpcConfig"]
    assert e.resolve(vpc["SubnetIds"]) == ["subnet-a", "subnet-b"] and e.resolve(vpc["SecurityGroupIds"]) == ["sg-1"]
    assert template["Outputs"]["AttachedVpcId"]["Value"] == {"Ref": "WorkerVpcId"}


def test_the_settings_of_the_old_shapes_are_gone(template):
    for name in REMOVED_PARAMETERS:
        assert name not in template["Parameters"], name
    assert set(template.get("Rules", {})) == {"IdpAllOrNothing"}
    assert set(template["Conditions"]) == {"HasIdp", "IdpRegionGiven", "HasIdpEvents", "HasApi", "HasIdpKeys", "LogsEventsToS3"}


# ---- US2: the one function carries every entry point ---------------------------------------------------------------------
def test_the_five_api_routes_proxy_to_the_one_function_and_only_the_schedule_is_an_event(template):
    events = fn(template)["Events"]
    assert [e["Type"] for e in events.values()] == ["Schedule"]  # events cannot be conditional: the API holds its own routes
    schedule = next(iter(events.values()))["Properties"]
    assert schedule["Schedule"] == "rate(5 minutes)" and schedule["Input"] == '{"action":"sweep"}'
    paths = template["Resources"]["OrderApi"]["Properties"]["DefinitionBody"]["paths"]
    assert {(path, method) for path, methods in paths.items() for method in methods} == {
        ("/confirmed-orders", "post"), ("/upload-jobs/{id}", "get"), ("/job-reservations", "post"),
        ("/job-reservations/{id}/save", "post"), ("/job-reservations/{id}", "get")}
    for methods in paths.values():
        for op in methods.values():
            integration = op["x-amazon-apigateway-integration"]
            assert integration["type"] == "aws_proxy" and integration["httpMethod"] == "POST"
            assert "${DirectFunction.Arn}/invocations" in integration["uri"]["Fn::Sub"]
            assert integration["credentials"]["Fn::Sub"].endswith(":iam::*:user/*")  # the caller's own credentials
    perm = template["Resources"]["OrderApiPermission"]["Properties"]
    assert perm["Principal"] == "apigateway.amazonaws.com" and perm["FunctionName"] == {"Ref": "DirectFunction"}


def test_the_private_api_uses_the_existing_endpoint_and_iam_auth(template):
    api = template["Resources"]["OrderApi"]["Properties"]
    assert template["Resources"]["OrderApi"]["Condition"] == "HasApi"
    assert api["EndpointConfiguration"] == {"Type": "PRIVATE", "VPCEndpointIds": [{"Ref": "ApiVpcEndpointId"}]}
    assert api["Auth"]["DefaultAuthorizer"] == "AWS_IAM"
    assert api["Auth"]["ResourcePolicy"]["IntrinsicVpcWhitelist"] == [{"Ref": "ApiVpcEndpointId"}]


def test_the_function_can_start_itself_by_a_name_that_needs_no_reference_to_it(template):
    assert fn(template)["FunctionName"] == {"Fn::Sub": "${AWS::StackName}-fn"}
    statements = [s for p in fn(template)["Policies"] if isinstance(p, dict) and "Statement" in p for s in p["Statement"]]
    invoke = [s for s in statements if s["Action"] == "lambda:InvokeFunction"]
    assert len(invoke) == 1
    resource = invoke[0]["Resource"]["Fn::Sub"]
    assert resource.endswith(":function:${AWS::StackName}-fn") and "DirectFunction" not in resource
    assert "WORKER_FUNCTION_NAME" not in env(template)  # the runtime's own name is used (config.worker_function_name)


# ---- US3: serialization and responsiveness -------------------------------------------------------------------------------
def test_no_reserved_concurrency_and_a_timeout_for_the_longest_save_and_no_async_retries(template):
    p = fn(template)
    assert "ReservedConcurrentExecutions" not in p  # it would throttle the API and the health check (FR-008)
    assert p["Timeout"] == 300 and p["EventInvokeConfig"]["MaximumRetryAttempts"] == 0
    assert env(template)["WORKER_TIMEOUT_SECONDS"] == "300"


def test_the_jobs_table_can_never_be_deleted_or_replaced_by_the_stack(template):
    table = template["Resources"]["JobsTable"]
    assert table["DeletionPolicy"] == "Retain" and table["UpdateReplacePolicy"] == "Retain"
    p = table["Properties"]
    assert p["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}]
    assert p["AttributeDefinitions"] == [{"AttributeName": "pk", "AttributeType": "S"}]
    assert p["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}
    assert p["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    assert template["Outputs"]["JobsTableName"]["Value"] == {"Ref": "JobsTable"}


def test_the_one_role_has_the_union_of_what_the_old_functions_needed(template):
    pols = fn(template)["Policies"]
    assert {"DynamoDBCrudPolicy": {"TableName": {"Ref": "JobsTable"}}} in pols
    secret = [s for p in pols if isinstance(p, dict) and "Statement" in p for s in p["Statement"]
              if s["Action"] == "secretsmanager:GetSecretValue"]
    assert secret and secret[0]["Resource"] == {"Ref": "SytelineSecretArn"}
    assert any("Fn::If" in p for p in pols)  # the IDP read permissions, only when the IDP is configured
    assert not any("LambdaInvokePolicy" in p for p in pols if isinstance(p, dict))  # only itself, by name


# ---- the optional document event rule --------------------------------------------------------------------------------------
def test_there_is_no_rule_unless_the_state_machine_arn_is_given_and_it_targets_the_function(template):
    assert Evaluator(template, **NET).condition("HasIdpEvents") is False
    assert Evaluator(template, IdpStateMachineArn=ARN, **NET).condition("HasIdpEvents") is True
    for name in ("IdpDocumentEventRule", "IdpDocumentEventPermission"):
        assert template["Resources"][name]["Condition"] == "HasIdpEvents"
    rule = template["Resources"]["IdpDocumentEventRule"]["Properties"]
    assert rule["EventPattern"] == {"source": ["aws.states"], "detail-type": ["Step Functions Execution Status Change"],
                                    "detail": {"status": ["SUCCEEDED"], "stateMachineArn": [{"Ref": "IdpStateMachineArn"}]}}
    assert rule["Targets"][0]["Arn"] == {"Fn::GetAtt": ["DirectFunction", "Arn"]}
    perm = template["Resources"]["IdpDocumentEventPermission"]["Properties"]
    assert perm["FunctionName"] == {"Ref": "DirectFunction"} and perm["Principal"] == "events.amazonaws.com"
    pattern = re.compile(template["Parameters"]["IdpStateMachineArn"]["AllowedPattern"])
    assert pattern.match("") and pattern.match(ARN) and not pattern.match("not-an-arn")


# ---- the settings the function carries (ported from the earlier per-function checks) -----------------------------------------
def test_the_route_tuning_and_encryption_settings_are_wired_to_the_function_with_the_codes_defaults(template):
    for name in ROUTE_VARS.values():
        p = template["Parameters"][name]
        assert p["Type"] == "String" and p["Default"] == "database" and "database" in p["Description"]
    assert {k: env(template)[k] for k in ROUTE_VARS} == {k: {"Ref": v} for k, v in ROUTE_VARS.items()}
    for name, default in CODE_DEFAULTS.items():
        assert template["Parameters"][name]["Default"] == default, name
    assert {k: env(template)[k] for k in TUNING_VARS} == {k: {"Ref": v} for k, v in TUNING_VARS.items()}
    enc = template["Parameters"]["SqlEncrypt"]
    assert enc["Default"] == "true" and enc["AllowedValues"] == ["true", "false"]
    assert "UNENCRYPTED" in enc["Description"] and "Force Encryption" in enc["Description"]
    assert env(template)["SYTELINE_SQL_ENCRYPT"] == {"Ref": "SqlEncrypt"}
    assert "SYTELINE_DB_CHECK" not in env(template) and "SYTELINE_ATTACHED" not in env(template)


def test_the_idp_and_save_settings_are_wired_and_the_idp_parameters_are_all_or_nothing(template):
    for k, v in (("IDP_STACK_NAME", "IdpStackName"), ("IDP_REGION", "IdpRegion"), ("IDP_DOCUMENT_CLASS", "IdpDocumentClass"),
                 ("IDP_FIELD_MAP", "IdpFieldMap"), ("SYTELINE_DEFAULT_WHSE", "DefaultWhse"),
                 ("SYTELINE_DEFAULT_ORDTYPE", "DefaultOrdType")):
        assert env(template)[k] == {"Ref": v}, k
    for name in ("IdpStackName", "IdpRegion", "IdpOutputBucketName", "IdpTrackingTableName", "IdpDocumentClass", "IdpFieldMap"):
        assert template["Parameters"][name]["Default"] == ""
    assert "Supply all three" in template["Rules"]["IdpAllOrNothing"]["Assertions"][0]["AssertDescription"]
    assert Evaluator(template, **NET).condition("HasIdp") is False
    assert Evaluator(template, IdpStackName="idp", **NET).condition("HasIdp") is True


def test_the_idp_permissions_include_the_lookup_function_of_the_idp_stack_and_only_that_function(template):
    cond = next(p for p in fn(template)["Policies"] if isinstance(p, dict) and "Fn::If" in p)["Fn::If"]
    assert cond[0] == "HasIdp" and cond[2] == {"Ref": "AWS::NoValue"}
    statements = cond[1]["Statement"]
    invoke = [s for s in statements if s["Action"] == "lambda:InvokeFunction"]
    assert len(invoke) == 1  # nothing broader than the one function the SDK needs
    resource = invoke[0]["Resource"]["Fn::Sub"][0]
    assert resource.endswith(":function:${IdpStackName}-LookupFunction-*") and resource.count("*") == 1
    assert ":lambda:${IdpRegionName}:${AWS::AccountId}:" in resource
    assert invoke[0]["Effect"] == "Allow"
    # the other IDP read permissions are still there
    actions = {a for s in statements for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])}
    assert {"cloudformation:DescribeStacks", "s3:GetObject", "dynamodb:GetItem"} <= actions


KMS_A = "arn:aws-us-gov:kms:us-gov-east-1:108375521309:key/1234abcd-12ab-34cd-56ef-1234567890ab"
KMS_B = "arn:aws-us-gov:kms:us-gov-east-1:108375521309:key/aaaabbbb-12ab-34cd-56ef-1234567890ab"


def test_the_kms_decrypt_permission_exists_only_for_the_keys_given_and_only_for_decrypt(template):
    param = template["Parameters"]["IdpKmsKeyArns"]
    assert param["Default"] == ""
    pattern = re.compile(param["AllowedPattern"])
    for good in ("", KMS_A, KMS_A + "," + KMS_B):
        assert pattern.match(good), good
    for bad in ("not-an-arn", KMS_A + ", " + KMS_B, KMS_A + ",", "arn:aws-us-gov:kms:us-gov-east-1:1:alias/x", KMS_A.replace("key/", "")):
        assert not pattern.match(bad), bad
    assert Evaluator(template, **NET).condition("HasIdpKeys") is False
    assert Evaluator(template, IdpKmsKeyArns=KMS_A, **NET).condition("HasIdpKeys") is True
    cond = [p for p in fn(template)["Policies"] if isinstance(p, dict) and "Fn::If" in p and p["Fn::If"][0] == "HasIdpKeys"]
    assert len(cond) == 1 and cond[0]["Fn::If"][2] == {"Ref": "AWS::NoValue"}
    statement = cond[0]["Fn::If"][1]["Statement"]
    assert len(statement) == 1 and statement[0]["Effect"] == "Allow" and statement[0]["Action"] == "kms:Decrypt"
    assert statement[0]["Resource"] == {"Fn::Split": [",", {"Ref": "IdpKmsKeyArns"}]}  # exactly the keys given, nothing broader


def test_the_erp_secret_and_live_writes_settings_are_unchanged(template):
    assert {"SYTELINE_WSDL", "SYTELINE_USER", "SYTELINE_CONFIG", "SYTELINE_SECRET_ARN", "SYTELINE_LIVE_WRITES"} <= set(env(template))
    assert template["Parameters"]["LiveWrites"]["Default"] == "false"
    assert template["Parameters"]["UseFipsEndpoints"]["Default"] == "true"


def test_the_outputs_name_the_one_function_the_api_and_the_table(template):
    assert set(template["Outputs"]) == {"DirectFunctionName", "FunctionArn", "ApiId", "JobsTableName", "AttachedVpcId", "EventCopyBucketName"}


def test_the_function_has_a_makefile_build_target_and_the_idp_sdk_is_packaged_from_a_checkout(template):
    makefile = open(os.path.join(SRC, "Makefile")).read()
    listed = set(re.search(r"^FUNCTIONS\s*=((?:.*\\\n)*.*)$", makefile, re.M).group(1).replace("\\", " ").split())
    assert listed == {"DirectFunction"} and "IDP_REPO" in makefile and "idp_sdk" in makefile and "idp_common" in makefile
    for f in ("requirements.txt", "requirements-idp.txt"):  # the squatted PyPI names must never be requested
        lines = [l.split("#")[0].strip().lower() for l in open(os.path.join(SRC, f))]
        assert not [l for l in lines if l.replace("_", "-").startswith(("idp-common", "idp-sdk"))]


def test_every_numeric_default_is_a_value_the_code_accepts():
    from syteline import config

    assert config.match_settings() == (70.0, 3.0, 1000) and config.sql_timeouts() == (5.0, 10.0) and config.site_id() == 10


# ---- feature 006, FR-016: the entry-log setting ------------------------------------------------------------------------
def test_log_events_is_an_opt_in_parameter_off_by_default_and_reaches_the_function(template):
    p = template["Parameters"]["LogEvents"]
    assert p["Default"] == "false" and p["AllowedValues"] == ["false", "true"]
    assert "document and order content" in p["Description"]
    assert env(template)["LOG_EVENTS"] == {"Ref": "LogEvents"}


def test_the_entry_log_adds_no_permission_and_no_resource(template):
    assert len(of_type(template, "AWS::Serverless::Function")) == 1
    assert not any("logs:" in str(s) for s in fn(template)["Policies"])


def test_job_number_is_an_empty_by_default_test_parameter_that_reaches_the_function(template):
    p = template["Parameters"]["JobNumber"]
    assert p["Default"] == "" and "TEST" in p["Description"]
    import re
    assert re.match(p["AllowedPattern"], "") and re.match(p["AllowedPattern"], "TEST-001") and not re.match(p["AllowedPattern"], "a b")
    assert env(template)["JOB_NUMBER"] == {"Ref": "JobNumber"}


def test_dev_target_co_is_an_empty_by_default_parameter_that_reaches_the_function(template):
    import re
    p = template["Parameters"]["DevTargetCo"]
    assert p["Default"] == "" and "DEVELOPMENT" in p["Description"] and "CoNumber" not in template["Parameters"]
    assert re.match(p["AllowedPattern"], "") and re.match(p["AllowedPattern"], "129999") and not re.match(p["AllowedPattern"], "a b")
    assert env(template)["DEV_TARGET_CO"] == {"Ref": "DevTargetCo"} and "CO_NUMBER" not in env(template)


# ---- feature 006 FR-016 revised: the bucket for oversized logged events (research R29) -------------------------------------
def test_the_event_copy_bucket_exists_only_while_the_whole_event_is_logged(template):
    assert template["Conditions"]["LogsEventsToS3"] == {"Fn::Equals": [{"Ref": "LogEvents"}, "true"]}
    assert Evaluator(template, **NET).condition("LogsEventsToS3") is False
    assert Evaluator(template, LogEvents="true", **NET).condition("LogsEventsToS3") is True
    for name in ("EventCopyBucket", "EventCopyBucketPolicy"):
        assert template["Resources"][name]["Condition"] == "LogsEventsToS3"
    assert env(template)["EVENT_COPY_BUCKET"] == {"Fn::If": ["LogsEventsToS3", {"Ref": "EventCopyBucket"}, ""]}


def test_the_event_copy_bucket_is_private_encrypted_expiring_and_retained(template):
    b = template["Resources"]["EventCopyBucket"]
    assert b["Type"] == "AWS::S3::Bucket" and b["DeletionPolicy"] == "Retain" and b["UpdateReplacePolicy"] == "Retain"
    props = b["Properties"]
    assert props["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    assert props["PublicAccessBlockConfiguration"] == {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                                       "IgnorePublicAcls": True, "RestrictPublicBuckets": True}
    (rule,) = props["LifecycleConfiguration"]["Rules"]
    assert rule["Status"] == "Enabled" and rule["ExpirationInDays"] == {"Ref": "EventCopyRetentionDays"}
    assert "VersioningConfiguration" not in props and "BucketName" not in props  # expiry must really delete; no fixed name


def test_the_event_copy_bucket_refuses_requests_without_tls(template):
    pol = template["Resources"]["EventCopyBucketPolicy"]["Properties"]
    (st,) = pol["PolicyDocument"]["Statement"]
    assert st["Effect"] == "Deny" and st["Action"] == "s3:*" and st["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_the_retention_is_a_bounded_number_defaulting_to_30_days(template):
    p = template["Parameters"]["EventCopyRetentionDays"]
    assert (p["Type"], p["Default"], p["MinValue"], p["MaxValue"]) == ("Number", 30, 1, 365)


def test_the_function_may_only_put_objects_under_events_in_that_bucket(template):
    cond = [p for p in fn(template)["Policies"] if isinstance(p, dict) and "Fn::If" in p and p["Fn::If"][0] == "LogsEventsToS3"]
    assert len(cond) == 1 and cond[0]["Fn::If"][2] == {"Ref": "AWS::NoValue"}
    (st,) = cond[0]["Fn::If"][1]["Statement"]
    assert st["Effect"] == "Allow" and st["Action"] == "s3:PutObject"
    assert st["Resource"] == {"Fn::Sub": "arn:${AWS::Partition}:s3:::${EventCopyBucket}/events/*"}


def test_the_bucket_name_is_an_output_only_when_it_exists(template):
    assert template["Outputs"]["EventCopyBucketName"] == {
        "Condition": "LogsEventsToS3", "Value": {"Ref": "EventCopyBucket"},
        "Description": template["Outputs"]["EventCopyBucketName"]["Description"]}
