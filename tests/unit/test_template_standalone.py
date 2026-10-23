"""src/template-standalone.yaml: no VPC, no public address, HTTPS by default (FR-001..003, FR-006..008)."""

import pytest

from cfn_eval import NOVALUE, Evaluator
from cfn_template import load_template

HTTPS = "https://erp.example.com/IDORequestService/IDOWebService.asmx?wsdl"
HTTP = "http://erp.example.com/IDORequestService/IDOWebService.asmx?wsdl"
BASE = dict(SytelineUser="svc", SytelineSecretArn="arn:aws-us-gov:secretsmanager:us-gov-east-1:1:secret:x")


def ev(t, **p):
    return Evaluator(t, **{**BASE, **p})


def fn(t):
    return t["Resources"]["DirectFunction"]["Properties"]


# ---- structure (US1) --------------------------------------------------------------------
ATTACH = dict(AttachVpcId="vpc-0abc1234", AttachSubnetIds="subnet-0a1,subnet-0b2", AttachSecurityGroupIds="sg-0c3")


def test_resources_are_exactly_the_layer_the_role_and_the_function(standalone_template):
    assert set(standalone_template["Resources"]) == {"DepsLayer", "DirectFunctionRole", "DirectFunction"}
    assert standalone_template["Resources"]["DepsLayer"]["Type"] == "AWS::Serverless::LayerVersion"
    assert standalone_template["Resources"]["DirectFunction"]["Type"] == "AWS::Serverless::Function"


def test_logical_ids_match_the_vpc_template_so_one_makefile_builds_both(standalone_template):
    vpc = load_template()
    for name in ("DepsLayer", "DirectFunction"):
        assert name in vpc["Resources"]
        assert standalone_template["Resources"][name]["Metadata"] == {"BuildMethod": "makefile"}
    for key in ("Handler", "Runtime", "Architectures", "CodeUri"):
        assert fn(standalone_template)[key] == vpc["Resources"]["DirectFunction"]["Properties"].get(
            key, vpc["Globals"]["Function"].get(key))


def test_no_vpc_mode_has_no_vpc_configuration_and_no_network_resource(standalone_template):
    e = ev(standalone_template, SytelineWsdlHttps=HTTPS)
    assert e.resolve(fn(standalone_template)["VpcConfig"]) is NOVALUE  # AWS::NoValue: not attached to any VPC
    assert e.condition("AttachToVpc") is False
    for name, r in standalone_template["Resources"].items():
        assert not r["Type"].startswith("AWS::EC2::"), f"{name} is {r['Type']}"


def test_only_the_three_attachment_parameters_exist_as_network_settings(standalone_template):
    bad = [p for p in standalone_template["Parameters"]
           if p.startswith(("Worker", "Host", "Plant")) or p in (
               "ApiVpcEndpointId", "CreateApiEndpoint", "CreateVpcEndpoints", "DynamoDbPrefixListId")]
    assert bad == []
    assert {"AttachVpcId", "AttachSubnetIds", "AttachSecurityGroupIds"} <= set(standalone_template["Parameters"])
    for p in ("AttachVpcId", "AttachSubnetIds", "AttachSecurityGroupIds"):
        assert standalone_template["Parameters"][p]["Default"] == ""


def test_it_can_coexist_with_the_vpc_stack(standalone_template):
    vpc = load_template()
    assert standalone_template["Resources"]["DepsLayer"]["Properties"]["LayerName"] != \
        vpc["Resources"]["DepsLayer"]["Properties"]["LayerName"]
    text = str(standalone_template)
    assert "Export" not in text and "ImportValue" not in text and "JobsTable" not in text
    assert "JOBS_TABLE" not in fn(standalone_template)["Environment"]["Variables"]


# ---- exposure (US3) ---------------------------------------------------------------------
def test_no_public_exposure(standalone_template):
    f = fn(standalone_template)
    assert "Events" not in f and "FunctionUrlConfig" not in f
    types = {r["Type"] for r in standalone_template["Resources"].values()}
    assert not types & {"AWS::Lambda::Url", "AWS::Lambda::Permission", "AWS::Serverless::Api",
                        "AWS::Serverless::HttpApi", "AWS::ApiGateway::RestApi", "AWS::ApiGatewayV2::Api"}
    assert "*" not in str({k: v for k, v in f.items() if k != "Policies"})


def role(t):
    return t["Resources"]["DirectFunctionRole"]["Properties"]


def test_the_function_uses_the_explicit_least_privilege_role(standalone_template):
    assert fn(standalone_template)["Role"] == {"Fn::GetAtt": ["DirectFunctionRole", "Arn"]}
    assert "Policies" not in fn(standalone_template)  # SAM would add the VPC permission in both modes
    trust = role(standalone_template)["AssumeRolePolicyDocument"]["Statement"]
    assert trust == [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]


def test_the_only_permission_besides_logging_is_reading_the_one_secret(standalone_template):
    pols = role(standalone_template)["Policies"]
    assert len(pols) == 1
    st = pols[0]["PolicyDocument"]["Statement"]
    assert len(st) == 1 and st[0]["Effect"] == "Allow"
    assert st[0]["Action"] == "secretsmanager:GetSecretValue" and st[0]["Resource"] == {"Ref": "SytelineSecretArn"}


def managed(t, **p):
    e = ev(t, SytelineWsdlHttps=HTTPS, **p)
    return [m.rsplit("/", 1)[-1] for m in e.resolve(role(t)["ManagedPolicyArns"])]


def test_network_interface_permission_is_granted_only_when_attached(standalone_template):
    assert managed(standalone_template) == ["AWSLambdaBasicExecutionRole"]
    assert managed(standalone_template, **ATTACH) == ["AWSLambdaBasicExecutionRole", "AWSLambdaVPCAccessExecutionRole"]


# ---- US4: attach to an existing network (FR-012, FR-002) -------------------------------
def test_attached_mode_uses_exactly_the_supplied_subnets_and_security_groups(standalone_template):
    e = ev(standalone_template, SytelineWsdlHttps=HTTPS, **ATTACH)
    assert e.violations() == [] and e.parameter_errors() == [] and e.condition("AttachToVpc") is True
    assert e.resolve(fn(standalone_template)["VpcConfig"]) == {
        "SubnetIds": ["subnet-0a1", "subnet-0b2"], "SecurityGroupIds": ["sg-0c3"]}


@pytest.mark.parametrize("missing", ["AttachVpcId", "AttachSubnetIds", "AttachSecurityGroupIds"])
def test_a_partial_attachment_is_refused_naming_all_three(standalone_template, missing):
    p = {**ATTACH, missing: ""}
    msgs = ev(standalone_template, SytelineWsdlHttps=HTTPS, **p).violations()
    assert any("Supply all three of AttachVpcId, AttachSubnetIds and AttachSecurityGroupIds" in m for m in msgs)


@pytest.mark.parametrize("name,value,ok", [
    ("AttachVpcId", "vpc-0abc1234", True), ("AttachVpcId", "subnet-0a1", False), ("AttachVpcId", "vpc-", False),
    ("AttachSubnetIds", "subnet-0a1", True), ("AttachSubnetIds", "subnet-0a1,subnet-0b2", True),
    ("AttachSubnetIds", "subnet-0a1, subnet-0b2", False), ("AttachSubnetIds", "sg-0c3", False),
    ("AttachSecurityGroupIds", "sg-0c3,sg-0d4", True), ("AttachSecurityGroupIds", "subnet-1", False)])
def test_attachment_identifier_patterns(standalone_template, name, value, ok):
    assert (ev(standalone_template, SytelineWsdlHttps=HTTPS, **{name: value}).parameter_errors() == []) is ok


def test_attaching_adds_no_way_to_call_the_functions_and_creates_no_network(standalone_template):
    e = ev(standalone_template, SytelineWsdlHttps=HTTPS, **ATTACH)
    assert e.active() == {"DepsLayer", "DirectFunctionRole", "DirectFunction"}
    f = fn(standalone_template)
    assert "Events" not in f and "FunctionUrlConfig" not in f
    assert not [r for r in standalone_template["Resources"].values() if r["Type"].startswith("AWS::EC2::")]


def test_the_https_rule_and_safe_defaults_hold_in_attached_mode_too(standalone_template):
    assert any("needs AllowPlainHttp=true" in m for m in
               ev(standalone_template, SytelineWsdlHttp=HTTP, **ATTACH).violations())
    assert ev(standalone_template, SytelineWsdlHttp=HTTP, AllowPlainHttp="true", **ATTACH).violations() == []
    env = fn(standalone_template)["Environment"]["Variables"]
    assert env["SYTELINE_REQUIRE_HTTPS"] == "true" and env["SYTELINE_LIVE_WRITES"] == {"Ref": "LiveWrites"}


def test_network_mode_outputs(standalone_template):
    out = standalone_template["Outputs"]
    plain = ev(standalone_template, SytelineWsdlHttps=HTTPS)
    att = ev(standalone_template, SytelineWsdlHttps=HTTPS, **ATTACH)
    assert plain.resolve(out["NetworkMode"]["Value"]) == "no-vpc" and att.resolve(out["NetworkMode"]["Value"]) == "attached"
    assert out["AttachedVpcId"]["Condition"] == "AttachToVpc" and att.condition("AttachToVpc")
    assert not plain.condition("AttachToVpc")


def test_safe_defaults(standalone_template):
    p = standalone_template["Parameters"]
    assert p["LiveWrites"]["Default"] == "false" and p["VerifyTls"]["Default"] == "true"
    assert p["AllowPlainHttp"]["Default"] == "false" and p["UseFipsEndpoints"]["Default"] == "true"
    env = fn(standalone_template)["Environment"]["Variables"]
    assert env["SYTELINE_REQUIRE_HTTPS"] == "true" and env["SYTELINE_ALLOW_HTTP"] == {"Ref": "AllowPlainHttp"}
    assert env["SYTELINE_VERIFY_TLS"] == {"Ref": "VerifyTls"} and env["SYTELINE_LIVE_WRITES"] == {"Ref": "LiveWrites"}
    assert not [k for k in env if "PASS" in k.upper()], "the password must come only from the secret"
    assert env["SYTELINE_SECRET_ARN"] == {"Ref": "SytelineSecretArn"}


# ---- HTTPS rules (US3) ------------------------------------------------------------------
def test_https_address_alone_is_accepted(standalone_template):
    e = ev(standalone_template, SytelineWsdlHttps=HTTPS)
    assert e.violations() == [] and e.parameter_errors() == []
    assert e.resolve(fn(standalone_template)["Environment"]["Variables"]["SYTELINE_WSDL"]) == HTTPS


def test_http_address_with_the_override_is_accepted(standalone_template):
    e = ev(standalone_template, SytelineWsdlHttp=HTTP, AllowPlainHttp="true")
    assert e.violations() == [] and e.parameter_errors() == []
    assert e.resolve(fn(standalone_template)["Environment"]["Variables"]["SYTELINE_WSDL"]) == HTTP


def test_http_address_without_the_override_is_refused(standalone_template):
    msgs = ev(standalone_template, SytelineWsdlHttp=HTTP).violations()
    assert any("needs AllowPlainHttp=true" in m and "unencrypted" in m for m in msgs)


@pytest.mark.parametrize("params", [dict(), dict(SytelineWsdlHttps=HTTPS, SytelineWsdlHttp=HTTP, AllowPlainHttp="true")])
def test_none_or_both_addresses_are_refused(standalone_template, params):
    assert any("exactly one" in m for m in ev(standalone_template, **params).violations())


@pytest.mark.parametrize("name,value", [("SytelineWsdlHttps", HTTP), ("SytelineWsdlHttp", HTTPS),
                                        ("SytelineWsdlHttps", "ftp://erp/x"), ("SytelineWsdlHttps", "erp.example.com"),
                                        ("SytelineWsdlHttps", "https://user:pw@erp.example.com/x"),
                                        ("SytelineWsdlHttp", "http://user@erp.example.com/x"),
                                        ("SytelineWsdlHttps", "https://erp example.com")])
def test_wrong_scheme_in_the_wrong_parameter_or_credentials_in_the_address_fail_the_pattern(standalone_template, name, value):
    assert ev(standalone_template, **{name: value}).parameter_errors()


def test_outputs_state_the_exposure(standalone_template):
    out = standalone_template["Outputs"]
    assert {"DirectFunctionName", "DirectFunctionArn", "PlainHttpOverride", "LiveWrites"} <= set(out)
    assert out["PlainHttpOverride"]["Value"] == {"Ref": "AllowPlainHttp"}


def test_the_vpc_template_has_no_standalone_parameters_or_guard(standalone_template):
    vpc = load_template()
    assert "SytelineWsdlHttps" not in vpc["Parameters"]
    env = str(vpc["Resources"]["WorkerFunction"]) + str(vpc["Globals"])
    assert "SYTELINE_REQUIRE_HTTPS" not in env  # the plant deployment keeps working over HTTP (SC-005)


# ---- optional database connectivity check (feature 005, revision 2026-10-22) -----------------------------------------
DB_PARAMS = {"DatabaseCheck": "auto", "SqlEncrypt": "true", "SqlVerifyHostname": "true", "SqlLoginTimeout": "5"}
DB_ENV = {"SYTELINE_DB_CHECK": "DatabaseCheck", "SYTELINE_SQL_ENCRYPT": "SqlEncrypt",
          "SYTELINE_SQL_VERIFY_HOSTNAME": "SqlVerifyHostname", "SYTELINE_SQL_LOGIN_TIMEOUT": "SqlLoginTimeout"}


def test_the_database_parameters_default_to_off_and_to_the_safe_encrypted_mode(standalone_template):
    for name, default in DB_PARAMS.items():
        assert standalone_template["Parameters"][name]["Default"] == default, name
    assert ev(standalone_template, SytelineWsdlHttps=HTTPS).parameter_errors() == []


def test_the_function_gets_the_database_settings_and_knows_whether_it_is_attached(standalone_template):
    env = fn(standalone_template)["Environment"]["Variables"]
    for var, param in DB_ENV.items():
        assert env[var] == {"Ref": param}, var
    assert env["SYTELINE_ATTACHED"] == {"Fn::If": ["AttachToVpc", "true", "false"]}
    e = ev(standalone_template, SytelineWsdlHttps=HTTPS)
    assert e.resolve(env["SYTELINE_ATTACHED"]) == "false"
    assert ev(standalone_template, SytelineWsdlHttps=HTTPS, **ATTACH).resolve(env["SYTELINE_ATTACHED"]) == "true"


def test_no_route_variable_no_secret_value_and_no_network_rule_were_added(standalone_template):
    text = repr(standalone_template)
    for var in ("SYTELINE_NEXT_CO_IDO", "SYTELINE_NEXT_JOB_IDO", "SYTELINE_CUSTOMER_IDO", "SYTELINE_DEFAULTS_IDO"):
        assert var not in text
    assert not [r for r in standalone_template["Resources"].values() if r["Type"].startswith("AWS::EC2::")]
    env = fn(standalone_template)["Environment"]["Variables"]
    assert not [k for k in env if k in ("SQL_PASS", "SQL_USER", "SQL_SERVER", "SQL_CA_PEM")]  # the login stays in the secret


def test_database_check_without_a_vpc_attachment_is_refused_naming_all_three(standalone_template):
    msgs = ev(standalone_template, SytelineWsdlHttps=HTTPS, DatabaseCheck="true").violations()
    assert any("AttachVpcId" in m and "AttachSubnetIds" in m and "AttachSecurityGroupIds" in m and "DatabaseCheck" in m
               for m in msgs)
    assert ev(standalone_template, SytelineWsdlHttps=HTTPS, DatabaseCheck="true", **ATTACH).violations() == []
    for free in ("auto", "false"):  # only "true" forces the check, so only "true" needs the attachment
        assert ev(standalone_template, SytelineWsdlHttps=HTTPS, DatabaseCheck=free).violations() == []


def test_the_encryption_and_numeric_parameters_reject_bad_values(standalone_template):
    for p, v in (("SqlEncrypt", "maybe"), ("SqlVerifyHostname", "1"), ("DatabaseCheck", "yes"), ("DatabaseCheck", ""),
                 ("SqlLoginTimeout", "x")):
        assert ev(standalone_template, SytelineWsdlHttps=HTTPS, **{p: v}).parameter_errors(), p


def test_database_check_accepts_exactly_auto_true_and_false(standalone_template):
    assert standalone_template["Parameters"]["DatabaseCheck"]["AllowedValues"] == ["auto", "true", "false"]
    for ok in ("auto", "true", "false"):
        assert ev(standalone_template, SytelineWsdlHttps=HTTPS, DatabaseCheck=ok, **ATTACH).parameter_errors() == []
