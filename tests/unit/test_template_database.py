"""The route settings and the database egress rules of src/template.yaml (specs/005-sql-server-calls/plan.md)."""

import os

from cfn_eval import Evaluator
from cfn_template import load_template

SUPPLY = dict(WorkerVpcId="vpc-1", WorkerSubnetIds="subnet-a,subnet-b", WorkerSecurityGroupIds="sg-1",
              WorkerRouteTableIds="rtb-1", ApiVpcEndpointId="vpce-1")
CREATE = dict(HostNetworkCidr="10.50.0.0/24", PlantCidr1="10.9.0.0/16", ApiVpcEndpointId="vpce-1")
ROUTE_VARS = {"SYTELINE_NEXT_CO_IDO": "NextCoIdo", "SYTELINE_NEXT_JOB_IDO": "NextJobIdo",
              "SYTELINE_CUSTOMER_IDO": "CustomerIdo", "SYTELINE_DEFAULTS_IDO": "DefaultsIdo"}


def env_of(template, fn):
    return (template["Resources"][fn]["Properties"].get("Environment") or {}).get("Variables", {})


def test_the_four_route_parameters_default_to_the_database(template):
    for name in ROUTE_VARS.values():
        p = template["Parameters"][name]
        assert p["Type"] == "String" and p["Default"] == "database" and "database" in p["Description"]


# the functions that number, resolve or save: reserve, the worker and the direct-invoke Lambda (which also runs the event intake)
DB_FUNCTIONS = ("ReserveFunction", "WorkerFunction", "DirectFunction")


def test_the_route_variables_are_on_exactly_the_functions_that_number_resolve_or_save(template):
    for fn in DB_FUNCTIONS:
        env = env_of(template, fn)
        assert {k: env[k] for k in ROUTE_VARS} == {k: {"Ref": v} for k, v in ROUTE_VARS.items()}, fn
    for fn, r in template["Resources"].items():
        if r["Type"] == "AWS::Serverless::Function" and fn not in DB_FUNCTIONS:
            assert not set(ROUTE_VARS) & set(env_of(template, fn)), fn


def test_the_standalone_template_sets_no_route_variable_and_no_network_rule():
    standalone = load_template(os.path.join(os.path.dirname(__file__), "..", "..", "src", "template-standalone.yaml"))
    text = repr(standalone)
    assert not any(v in text for v in ROUTE_VARS)
    assert not [r for r in standalone["Resources"].values() if r["Type"].startswith("AWS::EC2::")]  # no rule: the deployer's group applies


def sql_rules(template, e):
    out = []
    for name in e.active():
        r = template["Resources"][name]
        if r["Type"] == "AWS::EC2::SecurityGroupEgress" and r["Properties"]["FromPort"] == 1433:
            p = r["Properties"]
            assert p["ToPort"] == 1433 and p["IpProtocol"] == "tcp"
            out.append(e.resolve(p["CidrIp"]))
    return sorted(out)


def test_database_egress_exists_only_when_the_network_is_created_and_only_to_the_plant_ranges(template):
    assert sql_rules(template, Evaluator(template, **SUPPLY)) == []  # the deployer's own security group applies
    assert sql_rules(template, Evaluator(template, **CREATE)) == ["10.9.0.0/16"]
    three = dict(CREATE, PlantCidr2="10.8.0.0/16", PlantCidr3="10.7.0.0/16")
    assert sql_rules(template, Evaluator(template, **three)) == ["10.7.0.0/16", "10.8.0.0/16", "10.9.0.0/16"]


def test_no_ingress_and_nothing_open_to_the_world_on_the_database_port(template):
    for name, r in template["Resources"].items():
        if r["Type"] == "AWS::EC2::SecurityGroupIngress":
            raise AssertionError(f"{name}: no ingress rule may exist")
        if r["Type"] == "AWS::EC2::SecurityGroupEgress" and r["Properties"]["FromPort"] == 1433:
            assert "0.0.0.0/0" not in repr(r["Properties"]["CidrIp"])


def test_no_new_function_and_no_new_permission_for_the_database(template):
    text = repr({n: r for n, r in template["Resources"].items() if r["Type"] == "AWS::Serverless::Function"})
    for known in list(TUNING_VARS) + list(TUNING_VARS.values()) + ["SYTELINE_SQL_ENCRYPT", "SqlEncrypt"]:
        text = text.replace(known, "")  # the one non-secret setting
    assert "sql" not in text.lower().replace("sytline", "")  # the login lives in the existing secret, already readable


def test_the_encryption_parameter_defaults_to_true_and_is_on_exactly_the_database_functions(template):
    p = template["Parameters"]["SqlEncrypt"]
    assert p["Default"] == "true" and p["AllowedValues"] == ["true", "false"]
    assert "UNENCRYPTED" in p["Description"] and "Force Encryption" in p["Description"]
    for fn in DB_FUNCTIONS:
        assert env_of(template, fn)["SYTELINE_SQL_ENCRYPT"] == {"Ref": "SqlEncrypt"}, fn
    for fn, r in template["Resources"].items():
        if r["Type"] == "AWS::Serverless::Function" and fn not in DB_FUNCTIONS:
            assert "SYTELINE_SQL_ENCRYPT" not in env_of(template, fn), fn


def test_the_standalone_template_has_the_encryption_setting_only_for_its_health_check():
    standalone = load_template(os.path.join(os.path.dirname(__file__), "..", "..", "src", "template-standalone.yaml"))
    env = standalone["Resources"]["DirectFunction"]["Properties"]["Environment"]["Variables"]
    assert env["SYTELINE_SQL_ENCRYPT"] == {"Ref": "SqlEncrypt"} and env["SYTELINE_DB_CHECK"] == {"Ref": "DatabaseCheck"}
    assert standalone["Parameters"]["DatabaseCheck"]["Default"] == "auto"  # attached: the stages show; not attached: unchanged


TUNING_VARS = {"SYTELINE_SITE_ID": "SiteId", "SYTELINE_MATCH_MIN_SCORE": "MatchMinScore",
               "SYTELINE_MATCH_MARGIN": "MatchMargin", "SYTELINE_MATCH_CAP": "MatchCap",
               "SYTELINE_SQL_VERIFY_HOSTNAME": "SqlVerifyHostname", "SYTELINE_SQL_LOGIN_TIMEOUT": "SqlLoginTimeout",
               "SYTELINE_SQL_TIMEOUT": "SqlTimeout"}
CODE_DEFAULTS = {"SiteId": "10", "MatchMinScore": "70", "MatchMargin": "3", "MatchCap": "1000",
                 "SqlVerifyHostname": "true", "SqlLoginTimeout": "5", "SqlTimeout": "10"}


def test_the_tuning_parameters_default_to_the_codes_own_defaults_and_are_wired_to_the_database_functions(template):
    for name, default in CODE_DEFAULTS.items():
        assert template["Parameters"][name]["Default"] == default, name
    for fn in DB_FUNCTIONS:
        env = env_of(template, fn)
        assert {k: env[k] for k in TUNING_VARS} == {k: {"Ref": v} for k, v in TUNING_VARS.items()}, fn


def test_the_idp_mapping_parameters_are_empty_by_default_and_wired_to_the_functions_that_read_idp(template):
    for name in ("IdpDocumentClass", "IdpFieldMap"):
        assert template["Parameters"][name]["Default"] == ""
    # reserve, save-intake, the worker and (specs/006) the direct-invoke Lambda, whose document_event action reads IDP
    for fn in ("ReserveFunction", "SaveIntakeFunction", "WorkerFunction", "DirectFunction"):
        env = env_of(template, fn)
        assert env["IDP_DOCUMENT_CLASS"] == {"Ref": "IdpDocumentClass"} and env["IDP_FIELD_MAP"] == {"Ref": "IdpFieldMap"}, fn
    for fn in ("IntakeFunction", "StatusFunction", "SweeperFunction", "ReservationStatusFunction"):
        assert "IDP_FIELD_MAP" not in env_of(template, fn), fn


def test_every_numeric_default_is_a_value_the_code_accepts(template):
    from syteline import config

    # the defaults really are what config.py falls back to, so an unset deploy.env changes nothing
    assert config.match_settings() == (70.0, 3.0, 1000) and config.sql_timeouts() == (5.0, 10.0) and config.site_id() == 10
