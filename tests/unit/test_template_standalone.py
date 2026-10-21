"""scr/template-standalone.yaml: no VPC, no public address, HTTPS by default (FR-001..003, FR-006..008)."""

import pytest

from cfn_eval import Evaluator
from cfn_template import load_template

HTTPS = "https://erp.example.com/IDORequestService/IDOWebService.asmx?wsdl"
HTTP = "http://erp.example.com/IDORequestService/IDOWebService.asmx?wsdl"
BASE = dict(SytelineUser="svc", SytelineSecretArn="arn:aws-us-gov:secretsmanager:us-gov-east-1:1:secret:x")


def ev(t, **p):
    return Evaluator(t, **{**BASE, **p})


def fn(t):
    return t["Resources"]["DirectFunction"]["Properties"]


# ---- structure (US1) --------------------------------------------------------------------
def test_resources_are_exactly_the_layer_and_the_function(standalone_template):
    assert set(standalone_template["Resources"]) == {"DepsLayer", "DirectFunction"}
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


def test_no_vpc_no_network_resource_of_any_kind(standalone_template):
    assert "VpcConfig" not in fn(standalone_template)
    assert "VpcConfig" not in str(standalone_template["Globals"]) if "Globals" in standalone_template else True
    for name, r in standalone_template["Resources"].items():
        assert not r["Type"].startswith("AWS::EC2::"), f"{name} is {r['Type']}"


def test_no_network_parameters_exist(standalone_template):
    bad = [p for p in standalone_template["Parameters"]
           if p.startswith(("Worker", "Host", "Plant")) or p in (
               "ApiVpcEndpointId", "CreateApiEndpoint", "CreateVpcEndpoints", "DynamoDbPrefixListId")]
    assert bad == []


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


def test_the_only_permission_is_reading_the_one_secret(standalone_template):
    pol = fn(standalone_template)["Policies"]
    assert len(pol) == 1
    st = pol[0]["Statement"]
    assert len(st) == 1 and st[0]["Effect"] == "Allow"
    assert st[0]["Action"] == "secretsmanager:GetSecretValue" and st[0]["Resource"] == {"Ref": "SytelineSecretArn"}


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
