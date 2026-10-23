"""src/deploy.sh behavior with stubbed `sam` and `aws` (contracts/template-interface.md)."""

import os
import re
import shutil
import stat
import subprocess

import pytest

SCR = os.path.join(os.path.dirname(__file__), "..", "..", "src")

BASE = {"SYTELINE_WSDL": "http://erp/IDORequestService/IDOWebService.asmx?wsdl", "SYTELINE_USER": "svc",
        "SYTELINE_SECRET_ARN": "arn:aws-us-gov:secretsmanager:us-gov-east-1:1:secret:x"}
SUPPLY = {"WORKER_VPC_ID": "vpc-1", "WORKER_SUBNET_IDS": "subnet-a,subnet-b", "WORKER_SG_IDS": "sg-1",
          "WORKER_ROUTE_TABLE_IDS": "rtb-1", "API_VPCE_ID": "vpce-1"}
CREATE = {"HOST_NETWORK_CIDR": "10.50.0.0/24", "PLANT_CIDR_1": "10.9.0.0/16", "API_VPCE_ID": "vpce-1"}

SAM_STUB = '#!/usr/bin/env bash\necho "SAM $*" >> "$STUB_LOG"\n'
AWS_STUB = '''#!/usr/bin/env bash
echo "AWS $*" >> "$STUB_LOG"
case "$*" in
  *get-secret-value*) [[ "${STUB_SECRET_FAIL:-}" == "1" ]] && exit 254; printf '%s' "$STUB_SECRET" ;;
  *put-secret-value*) f="$(printf '%s\n' "$@" | sed -n 's#^file://##p')"; cp "$f" "$STUB_PUT"; echo "tmpfile-mode $(stat -f %Lp "$f" 2>/dev/null || stat -c %a "$f")" >> "$STUB_LOG" ;;
  *list-profiles*) [[ "${STUB_PROFILES_FAIL:-}" == "1" ]] && exit 252; printf '%s\n' ${STUB_PROFILES:-default} ;;
  *StackStatus*) [[ -n "${STUB_STACK_STATUS:-}" ]] && echo "$STUB_STACK_STATUS" || exit 255 ;;
  *HostVpcId*) echo "${STUB_HOSTVPC:-None}" ;;
esac
'''


@pytest.fixture
def run(tmp_path):
    work = tmp_path / "src"
    work.mkdir()
    shutil.copy(os.path.join(SCR, "deploy.sh"), work / "deploy.sh")
    shutil.copy(os.path.join(SCR, "samconfig.toml"), work / "samconfig.toml")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("sam", SAM_STUB), ("aws", AWS_STUB)):
        f = bin_dir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log"
    log.write_text("")

    put_path = tmp_path / "put.json"

    def go(settings, args=(), stack_status="", host_vpc="", with_aws=True, profiles="default govcloud-prod",
           profiles_fail=False, env_name="deploy.env", write_env=True, secret=None, secret_fail=False):
        if write_env:
            (work / env_name).write_text("".join(f'{k}="{v}"\n' for k, v in settings.items()))
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(log), "STUB_STACK_STATUS": stack_status,
               "STUB_HOSTVPC": host_vpc, "STUB_PROFILES": profiles, "STUB_PROFILES_FAIL": "1" if profiles_fail else "",
               "STUB_PUT": str(put_path), "STUB_SECRET_FAIL": "1" if secret_fail else ""}
        env["STUB_SECRET"] = secret if secret is not None else '{"SYTELINE_PASS": "erp-pass"}'
        if not with_aws:
            (bin_dir / "aws").unlink()
        log.write_text("")
        p = subprocess.run(["bash", str(work / "deploy.sh"), *args], capture_output=True, text=True, env=env, cwd=work)
        calls = log.read_text()
        return p, calls

    go.put_path = put_path
    go.work = work
    return go


def overrides(calls):
    line = [l for l in calls.splitlines() if l.startswith("SAM deploy")]
    assert line, calls
    return dict(re.findall(r'(\w+)="([^"]*)"', line[0]))


def deployed(calls):
    return any(l.startswith("SAM deploy") for l in calls.splitlines())


# ---- US1: create mode --------------------------------------------------------------------
def test_create_mode_passes_new_parameters_and_none_of_the_identifiers(run):
    p, calls = run({**BASE, **CREATE, "PLANT_CIDR_2": "10.8.0.0/16", "DYNAMODB_PREFIX_LIST_ID": "pl-63a5400a"})
    assert p.returncode == 0, p.stderr
    o = overrides(calls)
    assert o["HostNetworkCidr"] == "10.50.0.0/24" and o["PlantCidr1"] == "10.9.0.0/16"
    assert o["PlantCidr2"] == "10.8.0.0/16" and "PlantCidr3" not in o and o["DynamoDbPrefixListId"] == "pl-63a5400a"
    assert o["ApiVpcEndpointId"] == "vpce-1" and o["CreateApiEndpoint"] == "false" and o["LiveWrites"] == "false"
    for k in ("WorkerVpcId", "WorkerSubnetIds", "WorkerSecurityGroupIds", "WorkerRouteTableIds", "CreateVpcEndpoints"):
        assert k not in o
    assert "overlap" in p.stdout


@pytest.mark.parametrize("drop,msg", [("HOST_NETWORK_CIDR", "HOST_NETWORK_CIDR is required"),
                                      ("PLANT_CIDR_1", "PLANT_CIDR_1 is required")])
def test_create_mode_needs_both_ranges(run, drop, msg):
    settings = {**BASE, **CREATE}
    settings[drop] = ""
    p, calls = run(settings)
    assert p.returncode != 0 and msg in p.stderr and not deployed(calls)


def test_create_api_endpoint_option_is_passed_when_creating(run):
    p, calls = run({**BASE, "HOST_NETWORK_CIDR": "10.50.0.0/24", "PLANT_CIDR_1": "10.9.0.0/16",
                    "CREATE_API_ENDPOINT": "true"})
    assert p.returncode == 0, p.stderr
    o = overrides(calls)
    assert o["CreateApiEndpoint"] == "true" and "ApiVpcEndpointId" not in o


def test_api_endpoint_is_required_unless_the_option_creates_one(run):
    p, calls = run({**BASE, "HOST_NETWORK_CIDR": "10.50.0.0/24", "PLANT_CIDR_1": "10.9.0.0/16"})
    assert p.returncode != 0 and "API_VPCE_ID is required" in p.stderr and not deployed(calls)


def test_option_with_an_existing_endpoint_is_contradictory(run):
    p, calls = run({**BASE, **CREATE, "CREATE_API_ENDPOINT": "true"})
    assert p.returncode != 0 and "cannot be combined" in p.stderr and not deployed(calls)


def test_option_needs_create_mode(run):
    p, calls = run({**BASE, **SUPPLY, "API_VPCE_ID": "", "CREATE_API_ENDPOINT": "true"})
    assert p.returncode != 0 and "only works while the host network is created" in p.stderr and not deployed(calls)


# ---- route settings (feature 005) -------------------------------------------------------------
ROUTE_KEYS = ("NextCoIdo", "NextJobIdo", "CustomerIdo", "DefaultsIdo")


def test_the_route_parameters_default_to_the_database_and_are_always_passed(run):
    p, calls = run({**BASE, **CREATE})
    assert p.returncode == 0, p.stderr
    o = overrides(calls)
    assert {k: o[k] for k in ROUTE_KEYS} == {k: "database" for k in ROUTE_KEYS}  # always given, so an old value is cleared


def test_an_endpoint_name_in_the_settings_keeps_that_operation_on_the_web_service(run):
    p, calls = run({**BASE, **CREATE, "NEXT_CO_IDO": "SLCos", "CUSTOMER_IDO": "SLCustAddrs"})
    o = overrides(calls)
    assert (o["NextCoIdo"], o["NextJobIdo"], o["CustomerIdo"], o["DefaultsIdo"]) == ("SLCos", "database", "SLCustAddrs",
                                                                                     "database")


def test_sql_encrypt_defaults_to_true_is_always_passed_and_false_warns(run):
    p, calls = run({**BASE, **CREATE})
    assert overrides(calls)["SqlEncrypt"] == "true" and "UNENCRYPTED" not in p.stdout
    p, calls = run({**BASE, **CREATE, "SQL_ENCRYPT": "false"})
    assert p.returncode == 0 and overrides(calls)["SqlEncrypt"] == "false" and "UNENCRYPTED" in p.stdout


def test_sql_encrypt_must_be_true_or_false_and_nothing_is_deployed_otherwise(run):
    p, calls = run({**BASE, **CREATE, "SQL_ENCRYPT": "maybe"})
    assert p.returncode != 0 and "SQL_ENCRYPT must be true or false" in (p.stdout + p.stderr) and not deployed(calls)


TUNING = {"SqlVerifyHostname": "true", "SqlLoginTimeout": "5", "SqlTimeout": "10", "SiteId": "10", "MatchMinScore": "70",
          "MatchMargin": "3", "MatchCap": "1000", "UseFipsEndpoints": "true", "IdpDocumentClass": "", "IdpFieldMap": ""}


def test_the_tuning_parameters_are_always_passed_with_the_codes_own_defaults(run):
    p, calls = run({**BASE, **CREATE})
    assert p.returncode == 0, p.stderr
    o = overrides(calls)
    assert {k: o[k] for k in TUNING} == TUNING  # always given, so an earlier value is cleared


def test_the_tuning_settings_are_passed_through(run):
    p, calls = run({**BASE, **CREATE, "SQL_VERIFY_HOSTNAME": "false", "SQL_LOGIN_TIMEOUT": "8", "SQL_TIMEOUT": "20",
                    "SITE_ID": "20", "MATCH_MIN_SCORE": "80.5", "MATCH_MARGIN": "5", "MATCH_CAP": "200",
                    "USE_FIPS_ENDPOINTS": "false", "IDP_DOCUMENT_CLASS": "PurchaseOrder"})
    o = overrides(calls)
    assert (o["SqlVerifyHostname"], o["SqlLoginTimeout"], o["SqlTimeout"], o["SiteId"]) == ("false", "8", "20", "20")
    assert (o["MatchMinScore"], o["MatchMargin"], o["MatchCap"]) == ("80.5", "5", "200")
    assert (o["UseFipsEndpoints"], o["IdpDocumentClass"]) == ("false", "PurchaseOrder")


@pytest.mark.parametrize("name,value,message", [
    ("SQL_VERIFY_HOSTNAME", "maybe", "SQL_VERIFY_HOSTNAME must be true or false"),
    ("USE_FIPS_ENDPOINTS", "1", "USE_FIPS_ENDPOINTS must be true or false"),
    ("SQL_TIMEOUT", "ten", "SQL_TIMEOUT must be a whole number"),
    ("MATCH_CAP", "1.5", "MATCH_CAP must be a whole number"),
    ("MATCH_MIN_SCORE", "high", "MATCH_MIN_SCORE must be a number")])
def test_a_bad_tuning_value_is_refused_before_any_sam_call(run, name, value, message):
    p, calls = run({**BASE, **CREATE, name: value})
    assert p.returncode != 0 and message in (p.stdout + p.stderr) and not deployed(calls)


def test_the_idp_field_map_json_keeps_its_quotes_through_sams_parser(run):
    p, calls = run({**BASE, **CREATE, "IDP_FIELD_MAP": '{\\"po_number\\":\\"purchase_order\\"}'})
    assert p.returncode == 0, p.stdout + p.stderr
    line = [l for l in calls.splitlines() if l.startswith("SAM deploy")][0]
    assert 'IdpFieldMap="{\\"po_number\\":\\"purchase_order\\"}"' in line


def test_an_idp_field_map_that_is_not_a_json_object_is_refused(run):
    p, calls = run({**BASE, **CREATE, "IDP_FIELD_MAP": "{po_number:purchase_order}"})
    assert p.returncode != 0 and "IDP_FIELD_MAP must be a JSON object" in (p.stdout + p.stderr) and not deployed(calls)


# ---- US2: supply mode / partial sets / mode-switch guard ---------------------------------
def test_feature_001_style_deploy_env_deploys_with_the_same_parameters(run):
    p, calls = run({**BASE, **SUPPLY, "CREATE_VPC_ENDPOINTS": "true"})
    assert p.returncode == 0, p.stderr
    o = overrides(calls)
    assert o["WorkerVpcId"] == "vpc-1" and o["WorkerSubnetIds"] == "subnet-a,subnet-b"
    assert o["WorkerSecurityGroupIds"] == "sg-1" and o["WorkerRouteTableIds"] == "rtb-1"
    assert o["ApiVpcEndpointId"] == "vpce-1" and o["CreateVpcEndpoints"] == "true"
    assert "HostNetworkCidr" not in o and "PlantCidr1" not in o


@pytest.mark.parametrize("missing", ["WORKER_VPC_ID", "WORKER_SUBNET_IDS", "WORKER_SG_IDS", "WORKER_ROUTE_TABLE_IDS"])
def test_partial_set_is_refused_naming_what_is_missing(run, missing):
    settings = {**BASE, **SUPPLY}
    settings[missing] = ""
    p, calls = run(settings)
    assert p.returncode != 0 and missing in p.stderr and "none of them to create" in p.stderr
    assert not deployed(calls) and "SAM build" not in calls


def test_create_mode_settings_with_a_full_set_are_ignored_with_a_warning(run):
    p, calls = run({**BASE, **SUPPLY, "HOST_NETWORK_CIDR": "10.50.0.0/24", "PLANT_CIDR_1": "10.9.0.0/16"})
    assert p.returncode == 0
    assert "ignored" in p.stdout and "HOST_NETWORK_CIDR" in p.stdout
    assert "HostNetworkCidr" not in overrides(calls)


def test_switching_create_to_supply_is_refused_without_the_flag(run):
    p, calls = run({**BASE, **SUPPLY}, stack_status="CREATE_COMPLETE", host_vpc="vpc-0abc")
    assert p.returncode != 0 and "REPLACE the network" in p.stdout and "--allow-network-change" in p.stderr
    assert not deployed(calls)


def test_switching_supply_to_create_is_refused_without_the_flag(run):
    p, calls = run({**BASE, **CREATE}, stack_status="UPDATE_COMPLETE", host_vpc="None")
    assert p.returncode != 0 and "'supply'" in p.stdout and not deployed(calls)


def test_the_flag_allows_the_switch(run):
    p, calls = run({**BASE, **SUPPLY}, args=["--allow-network-change"], stack_status="CREATE_COMPLETE",
                   host_vpc="vpc-0abc")
    assert p.returncode == 0 and deployed(calls) and "--allow-network-change given" in p.stdout


def test_same_mode_update_is_not_blocked(run):
    p, calls = run({**BASE, **CREATE}, stack_status="UPDATE_COMPLETE", host_vpc="vpc-0abc")
    assert p.returncode == 0 and deployed(calls)
    p, calls = run({**BASE, **SUPPLY}, stack_status="CREATE_COMPLETE", host_vpc="None")
    assert p.returncode == 0 and deployed(calls)


def test_first_deploy_has_nothing_to_compare(run):
    p, calls = run({**BASE, **CREATE}, stack_status="")  # describe-stacks fails: no such stack
    assert p.returncode == 0 and deployed(calls)


def test_missing_aws_cli_warns_and_continues(run, tmp_path):
    p, calls = run({**BASE, **CREATE}, with_aws=False)
    assert p.returncode == 0 and "cannot check" in p.stdout


# ---- unchanged feature-001 behavior ------------------------------------------------------
def test_live_writes_still_need_an_approval_reference(run):
    p, calls = run({**BASE, **SUPPLY, "LIVE_WRITES": "true"})
    assert p.returncode != 0 and "LIVE_APPROVAL_REF" in p.stderr and not deployed(calls)
    p, calls = run({**BASE, **SUPPLY, "LIVE_WRITES": "true", "LIVE_APPROVAL_REF": "ERP-OWNER-123"})
    assert p.returncode == 0 and overrides(calls)["LiveWrites"] == "true"


def test_always_required_settings_are_checked(run):
    p, calls = run({"SYTELINE_USER": "svc", **SUPPLY})
    assert p.returncode != 0 and "SYTELINE_WSDL" in p.stderr and "SYTELINE_SECRET_ARN" in p.stderr


# ---- AWS credentials profile switch (FR-012) ---------------------------------------------
def profile_args_of(calls):
    """{step: set of profiles used} for the AWS-facing calls (None = no --profile on that call)."""
    out = {}
    for line in calls.splitlines():
        step = ("build" if line.startswith("SAM build") else "deploy" if line.startswith("SAM deploy")
                else "validate" if line.startswith("SAM validate")
                else "stack lookup" if "describe-stacks" in line else None)
        if step:
            m = re.search(r"--profile (\S+)", line)
            out.setdefault(step, set()).add(m.group(1) if m else None)
    return out


def test_no_profile_means_no_profile_flag_anywhere(run):
    p, calls = run({**BASE, **SUPPLY}, stack_status="CREATE_COMPLETE", host_vpc="None")
    assert p.returncode == 0 and "--profile" not in calls and "Using AWS profile" not in p.stdout


def test_profile_flag_reaches_every_aws_facing_step(run):
    p, calls = run({**BASE, **SUPPLY}, args=["--profile", "govcloud-prod"], stack_status="CREATE_COMPLETE",
                   host_vpc="None")
    assert p.returncode == 0 and "Using AWS profile: govcloud-prod" in p.stdout
    seen = profile_args_of(calls)
    assert seen == {"build": {"govcloud-prod"}, "deploy": {"govcloud-prod"}, "stack lookup": {"govcloud-prod"}}
    assert calls.count("--profile govcloud-prod") >= 4  # build, deploy and both stack look-ups


def test_equals_form_and_validate_and_build_modes(run):
    p, calls = run({**BASE, **SUPPLY}, args=["--profile=govcloud-prod", "--validate"])
    assert p.returncode == 0 and "SAM validate --lint --profile govcloud-prod" in calls
    p, calls = run({**BASE, **SUPPLY}, args=["--build", "--profile", "govcloud-prod"])
    assert p.returncode == 0 and "SAM build --profile govcloud-prod" in calls and not deployed(calls)


def test_profile_from_deploy_env_and_flag_wins(run):
    p, calls = run({**BASE, **SUPPLY, "AWS_PROFILE_NAME": "govcloud-prod"})
    assert p.returncode == 0 and "SAM deploy --profile govcloud-prod" in calls
    p, calls = run({**BASE, **SUPPLY, "AWS_PROFILE_NAME": "govcloud-prod"}, args=["--profile", "default"])
    assert p.returncode == 0 and "--profile default" in calls and "govcloud-prod" not in calls


def test_unknown_profile_is_refused_before_anything_runs(run):
    p, calls = run({**BASE, **SUPPLY}, args=["--profile", "nope"])
    assert p.returncode != 0 and "'nope' is not configured" in p.stderr
    assert "SAM" not in calls and "describe-stacks" not in calls


@pytest.mark.parametrize("bad", ["a b", "x;rm -rf", "$(id)", "p/q"])
def test_invalid_profile_names_are_refused(run, bad):
    p, calls = run({**BASE, **SUPPLY}, args=["--profile", bad])
    assert p.returncode != 0 and "invalid profile name" in p.stderr and "SAM" not in calls


@pytest.mark.parametrize("args", [["--profile"], ["--profile="], ["--profile", "--validate"]])
def test_profile_without_a_name_is_refused(run, args):
    p, calls = run({**BASE, **SUPPLY}, args=args)
    assert p.returncode != 0 and "needs a profile name" in p.stderr and "SAM" not in calls


def test_profile_check_is_skipped_with_a_warning_if_profiles_cannot_be_listed(run):
    p, calls = run({**BASE, **SUPPLY}, args=["--profile", "whatever"], profiles_fail=True)
    assert p.returncode == 0 and "could not list AWS profiles" in p.stdout and "--profile whatever" in calls


def test_profile_works_without_the_aws_cli_installed(run):
    p, calls = run({**BASE, **SUPPLY}, args=["--profile", "govcloud-prod"], with_aws=False)
    assert p.returncode == 0 and "SAM deploy --profile govcloud-prod" in calls


# ---- stand-alone (no VPC) mode ----------------------------------------------------------
SA = {"SYTELINE_WSDL": "https://erp.example.com/IDORequestService/IDOWebService.asmx?wsdl", "SYTELINE_USER": "svc",
      "SYTELINE_SECRET_ARN": "arn:aws-us-gov:secretsmanager:us-gov-east-1:1:secret:x"}
NETWORK_SETTINGS = ["WORKER_VPC_ID", "WORKER_SUBNET_IDS", "WORKER_SG_IDS", "WORKER_ROUTE_TABLE_IDS", "HOST_NETWORK_CIDR",
                    "PLANT_CIDR_1", "PLANT_CIDR_2", "PLANT_CIDR_3", "DYNAMODB_PREFIX_LIST_ID", "API_VPCE_ID",
                    "CREATE_API_ENDPOINT", "CREATE_VPC_ENDPOINTS"]


def sa(run, settings=None, args=(), **kw):
    return run({**SA, **(settings or {})}, args=["--standalone", *args], env_name="deploy.standalone.env", **kw)


def test_standalone_uses_its_own_config_environment_and_settings_file(run):
    p, calls = sa(run)
    assert p.returncode == 0, p.stderr
    assert "SAM build --config-env standalone" in calls and "SAM deploy --config-env standalone" in calls
    assert "describe-stacks" not in calls  # no create/supply mode guard
    o = overrides(calls)
    assert o["SytelineWsdlHttps"] == SA["SYTELINE_WSDL"] and o["SytelineWsdlHttp"] == ""  # the unused one is cleared (sam keeps old values)
    assert o["AllowPlainHttp"] == "false" and o["VerifyTls"] == "true" and o["LiveWrites"] == "false"
    assert o["SytelineUser"] == "svc" and o["SytelineConfig"] == "CCT"
    assert not any(k in o for k in ("WorkerVpcId", "HostNetworkCidr", "ApiVpcEndpointId", "CreateVpcEndpoints"))
    assert "STAND-ALONE" in p.stdout


def test_standalone_does_not_read_the_vpc_settings_file(run, tmp_path):
    p, calls = run({**SA}, args=["--standalone"], env_name="deploy.env")  # only deploy.env exists
    assert p.returncode != 0 and "deploy.standalone.env missing" in p.stdout and not deployed(calls)


def test_standalone_build_and_validate_need_no_settings_file(run):
    p, calls = run({}, args=["--standalone", "--validate"], write_env=False)
    assert p.returncode == 0 and "SAM validate --lint --config-env standalone" in calls
    p, calls = run({}, args=["--standalone", "--build"], write_env=False)
    assert p.returncode == 0 and "SAM build --config-env standalone" in calls and not deployed(calls)


def test_standalone_honors_the_profile_flag_and_the_settings_file(run):
    p, calls = sa(run, args=["--profile", "govcloud-prod"])
    assert p.returncode == 0 and "SAM deploy --config-env standalone --profile govcloud-prod" in calls
    p, calls = sa(run, {"AWS_PROFILE_NAME": "govcloud-prod"})
    assert p.returncode == 0 and "--profile govcloud-prod" in calls
    p, calls = sa(run, {"AWS_PROFILE_NAME": "govcloud-prod"}, args=["--profile", "default"])
    assert "--profile default" in calls and "govcloud-prod" not in calls


@pytest.mark.parametrize("name", NETWORK_SETTINGS)
def test_standalone_refuses_each_network_setting_as_not_applicable(run, name):
    p, calls = sa(run, {name: "x"})
    assert p.returncode != 0 and "not applicable to the stand-alone version" in p.stderr and name in p.stderr
    assert "SAM" not in calls


def test_standalone_missing_required_settings(run):
    p, calls = run({"SYTELINE_USER": "svc"}, args=["--standalone"], env_name="deploy.standalone.env")
    assert p.returncode != 0 and "SYTELINE_WSDL" in p.stderr and "SYTELINE_SECRET_ARN" in p.stderr


# -- HTTPS by default / override / TLS / approval (US3) --
def test_http_address_without_the_override_is_refused_before_any_sam_call(run):
    p, calls = sa(run, {"SYTELINE_WSDL": "http://erp.example.com/x?wsdl"})
    assert p.returncode != 0 and "ALLOW_PLAIN_HTTP=true" in p.stderr and "SAM" not in calls


STANDALONE_DB = {"DatabaseCheck": "auto", "SqlEncrypt": "true", "SqlVerifyHostname": "true", "SqlLoginTimeout": "5"}
ATT = {"ATTACH_VPC_ID": "vpc-0abc1234", "ATTACH_SUBNET_IDS": "subnet-0a1,subnet-0b2", "ATTACH_SG_IDS": "sg-0c3"}


def test_standalone_always_passes_the_database_settings_with_safe_defaults(run):
    p, calls = sa(run)
    assert p.returncode == 0, p.stderr
    o = overrides(calls)
    assert {k: o[k] for k in STANDALONE_DB} == STANDALONE_DB and "UNENCRYPTED" not in p.stdout


def test_standalone_database_check_needs_the_vpc_attachment_and_passes_through_when_attached(run):
    p, calls = sa(run, {"DATABASE_CHECK": "true"})
    assert p.returncode != 0 and "DATABASE_CHECK=true needs ATTACH_VPC_ID" in (p.stdout + p.stderr) and not deployed(calls)
    p, calls = sa(run, {"DATABASE_CHECK": "true", "SQL_ENCRYPT": "false", "SQL_VERIFY_HOSTNAME": "false",
                        "SQL_LOGIN_TIMEOUT": "9", **ATT})
    assert p.returncode == 0, p.stdout + p.stderr
    o = overrides(calls)
    assert (o["DatabaseCheck"], o["SqlEncrypt"], o["SqlVerifyHostname"], o["SqlLoginTimeout"]) == ("true", "false", "false", "9")
    assert "UNENCRYPTED" in p.stdout


@pytest.mark.parametrize("name,value,message", [("DATABASE_CHECK", "yes", "DATABASE_CHECK must be auto, true or false"),
                                                 ("SQL_ENCRYPT", "maybe", "SQL_ENCRYPT must be true or false"),
                                                 ("SQL_VERIFY_HOSTNAME", "1", "SQL_VERIFY_HOSTNAME must be true or false"),
                                                 ("SQL_LOGIN_TIMEOUT", "fast", "SQL_LOGIN_TIMEOUT must be a whole number")])
def test_standalone_refuses_bad_database_values_before_any_sam_call(run, name, value, message):
    p, calls = sa(run, {name: value})
    assert p.returncode != 0 and message in (p.stdout + p.stderr) and not deployed(calls)


def test_standalone_passes_use_fips_endpoints_and_refuses_a_bad_value(run):
    p, calls = sa(run)
    assert overrides(calls)["UseFipsEndpoints"] == "true"
    p, calls = sa(run, {"USE_FIPS_ENDPOINTS": "false"})
    assert overrides(calls)["UseFipsEndpoints"] == "false"
    p, calls = sa(run, {"USE_FIPS_ENDPOINTS": "x"})
    assert p.returncode != 0 and "USE_FIPS_ENDPOINTS must be true or false" in (p.stdout + p.stderr) and not deployed(calls)


def test_http_address_with_the_override_uses_the_http_parameter_and_warns(run):
    p, calls = sa(run, {"SYTELINE_WSDL": "http://erp.example.com/x?wsdl", "ALLOW_PLAIN_HTTP": "true"})
    assert p.returncode == 0, p.stderr
    o = overrides(calls)
    assert o["SytelineWsdlHttp"] == "http://erp.example.com/x?wsdl" and o["SytelineWsdlHttps"] == ""
    assert o["AllowPlainHttp"] == "true"
    assert "UNENCRYPTED" in p.stdout


def test_override_with_an_https_address_still_warns_that_https_is_not_enforced(run):
    p, calls = sa(run, {"ALLOW_PLAIN_HTTP": "true"})
    assert p.returncode == 0 and "no longer enforced" in p.stdout and overrides(calls)["AllowPlainHttp"] == "true"


@pytest.mark.parametrize("url", ["ftp://erp/x", "erp.example.com/x", "file:///etc/x.wsdl"])
def test_other_schemes_are_refused(run, url):
    p, calls = sa(run, {"SYTELINE_WSDL": url})
    assert p.returncode != 0 and "must start with https://" in p.stderr and "SAM" not in calls


@pytest.mark.parametrize("url", ["https://user:pw@erp.example.com/x", "http://user@erp.example.com/x"])
def test_credentials_in_the_address_are_refused(run, url):
    p, calls = sa(run, {"SYTELINE_WSDL": url, "ALLOW_PLAIN_HTTP": "true"})
    assert p.returncode != 0 and "must not contain credentials" in p.stderr and "pw" not in p.stdout + p.stderr
    assert "SAM" not in calls


def test_verify_tls_defaults_true_and_can_be_switched_off(run):
    assert overrides(sa(run)[1])["VerifyTls"] == "true"
    assert overrides(sa(run, {"VERIFY_TLS": "false"})[1])["VerifyTls"] == "false"
    p, calls = sa(run, {"VERIFY_TLS": "maybe"})
    assert p.returncode != 0 and "VERIFY_TLS" in p.stderr


def test_standalone_live_writes_need_an_approval_reference(run):
    p, calls = sa(run, {"LIVE_WRITES": "true"})
    assert p.returncode != 0 and "LIVE_APPROVAL_REF" in p.stderr and "SAM" not in calls
    p, calls = sa(run, {"LIVE_WRITES": "true", "LIVE_APPROVAL_REF": "ERP-OWNER-9"})
    assert p.returncode == 0 and overrides(calls)["LiveWrites"] == "true"


def test_the_default_mode_is_unchanged_by_the_standalone_option(run):
    p, calls = run({**BASE, **SUPPLY})
    assert p.returncode == 0 and "--config-env" not in calls and "SytelineWsdlHttps" not in calls


# ---- stand-alone: attach to an existing network (all three settings or none) -------------
ATTACH = {"ATTACH_VPC_ID": "vpc-0abc1234", "ATTACH_SUBNET_IDS": "subnet-0a1,subnet-0b2", "ATTACH_SG_IDS": "sg-0c3"}


def test_attached_settings_are_passed_and_no_other_network_resource_is_requested(run):
    p, calls = sa(run, ATTACH)
    assert p.returncode == 0, p.stderr
    o = overrides(calls)
    assert o["AttachVpcId"] == "vpc-0abc1234" and o["AttachSubnetIds"] == "subnet-0a1,subnet-0b2"
    assert o["AttachSecurityGroupIds"] == "sg-0c3"
    assert "ATTACHED to existing VPC vpc-0abc1234" in p.stdout and "Secrets Manager" in p.stdout
    assert not any(k in o for k in ("WorkerVpcId", "HostNetworkCidr", "ApiVpcEndpointId", "CreateVpcEndpoints"))


def test_no_attachment_means_no_attach_parameters(run):
    p, calls = sa(run)
    o = overrides(calls)
    assert not any(k.startswith("Attach") for k in o) and "no VPC" in p.stdout


@pytest.mark.parametrize("missing", list(ATTACH))
def test_a_partial_attachment_is_refused_naming_what_is_missing(run, missing):
    settings = {k: v for k, v in ATTACH.items() if k != missing}
    p, calls = sa(run, settings)
    assert p.returncode != 0 and missing in p.stderr and "or none of them to run outside any VPC" in p.stderr
    assert "SAM" not in calls


@pytest.mark.parametrize("name,value", [("ATTACH_VPC_ID", "subnet-0a1"), ("ATTACH_VPC_ID", "vpc-"),
                                        ("ATTACH_SUBNET_IDS", "subnet-0a1, subnet-0b2"), ("ATTACH_SUBNET_IDS", "sg-1"),
                                        ("ATTACH_SG_IDS", "subnet-1"), ("ATTACH_SG_IDS", "sg-1;rm")])
def test_malformed_attachment_identifiers_are_refused(run, name, value):
    p, calls = sa(run, {**ATTACH, name: value})
    assert p.returncode != 0 and name in p.stderr and "SAM" not in calls


def test_other_network_settings_are_still_not_applicable_even_when_attaching(run):
    p, calls = sa(run, {**ATTACH, "WORKER_ROUTE_TABLE_IDS": "rtb-1", "API_VPCE_ID": "vpce-1"})
    assert p.returncode != 0 and "not applicable" in p.stderr and "WORKER_ROUTE_TABLE_IDS" in p.stderr
    assert "SAM" not in calls


def test_attached_mode_keeps_the_https_rule_and_the_live_write_approval(run):
    p, calls = sa(run, {**ATTACH, "SYTELINE_WSDL": "http://erp.plant.local/x?wsdl"})
    assert p.returncode != 0 and "ALLOW_PLAIN_HTTP=true" in p.stderr and "SAM" not in calls
    p, calls = sa(run, {**ATTACH, "LIVE_WRITES": "true"})
    assert p.returncode != 0 and "LIVE_APPROVAL_REF" in p.stderr


def test_standalone_auto_and_false_need_no_vpc_attachment_and_true_does(run):
    for value in ("auto", "false"):
        p, calls = sa(run, {"DATABASE_CHECK": value})
        assert p.returncode == 0, (value, p.stdout + p.stderr)
        assert overrides(calls)["DatabaseCheck"] == value
    p, calls = sa(run, {"DATABASE_CHECK": "true"})
    assert p.returncode != 0 and not deployed(calls)
    p, calls = sa(run, {"DATABASE_CHECK": "true", **ATT})
    assert p.returncode == 0 and overrides(calls)["DatabaseCheck"] == "true"


# ---- the database settings are merged into the credentials secret (not passed to CloudFormation) ---------------------
import json  # noqa: E402

DB = {"SQL_SERVER": "sql.plant.example", "SQL_DATABASE": "SL_PROD", "SQL_USER": "svc-login", "SQL_PASS": "S3cr3t-DB-Pass",
      "SQL_PORT": "14330"}
SECRET_CALLS = ("get-secret-value", "put-secret-value")


def written(run):
    return json.loads(run.put_path.read_text())


def test_nothing_is_read_or_written_when_no_database_setting_is_given(run):
    p, calls = run({**BASE, **CREATE})
    assert p.returncode == 0 and not any(c in calls for c in SECRET_CALLS) and deployed(calls)


def test_the_database_values_are_merged_into_the_secret_keeping_its_other_keys(run):
    pem = run.work / "ca.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n")
    p, calls = run({**BASE, **CREATE, **DB, "SQL_CA_PEM_FILE": str(pem)}, args=["--yes"],
                   secret='{"SYTELINE_PASS": "erp-pass", "SQL_PORT": "1433", "EXTRA": "kept"}')
    assert p.returncode == 0, p.stdout + p.stderr
    out = written(run)
    assert out["SYTELINE_PASS"] == "erp-pass" and out["EXTRA"] == "kept"  # untouched keys survive
    assert out["SQL_SERVER"] == "sql.plant.example" and out["SQL_DATABASE"] == "SL_PROD"
    assert out["SQL_USER"] == "svc-login" and out["SQL_PASS"] == "S3cr3t-DB-Pass" and out["SQL_PORT"] == "14330"
    assert out["SQL_CA_PEM"].startswith("-----BEGIN CERTIFICATE-----") and out["SQL_CA_PEM"].endswith("-----\n")
    assert calls.index("get-secret-value") < calls.index("put-secret-value") < calls.index("SAM build")  # before building
    assert deployed(calls)


def test_only_the_given_keys_change(run):
    p, calls = run({**BASE, **CREATE, "SQL_SERVER": "new.plant.example"}, args=["--yes"],
                   secret='{"SQL_SERVER": "old", "SQL_USER": "keep-me", "SQL_PASS": "keep-pass"}')
    assert p.returncode == 0
    assert written(run) == {"SQL_SERVER": "new.plant.example", "SQL_USER": "keep-me", "SQL_PASS": "keep-pass"}


def test_the_values_never_reach_the_output_the_command_lines_or_the_stack_parameters(run):
    p, calls = run({**BASE, **CREATE, **DB}, args=["--yes"])
    assert p.returncode == 0
    for secret in (DB["SQL_PASS"], DB["SQL_USER"]):
        assert secret not in p.stdout + p.stderr and secret not in calls  # not printed, not in any logged command line
    assert "keys: SQL_SERVER SQL_DATABASE SQL_USER SQL_PASS SQL_PORT" in p.stdout  # key names only
    o = overrides(calls)
    assert not {"SqlServer", "SqlDatabase", "SqlUser", "SqlPass", "SqlPort", "SqlCaPem"} & set(o)  # no such stack parameter
    assert not set(DB.values()) & set(o.values())                                                  # and no such value
    deploy_line = [l for l in calls.splitlines() if l.startswith("SAM deploy")][0]
    assert not any(v in deploy_line for v in DB.values())


def test_the_temporary_file_is_private_and_removed(run):
    p, calls = run({**BASE, **CREATE, **DB}, args=["--yes"])
    assert "tmpfile-mode 600" in calls
    path = [a for a in calls.split() if a.startswith("file://")][0][len("file://"):]
    assert not os.path.exists(path)


def test_without_yes_and_without_a_terminal_it_refuses_and_changes_nothing(run):
    p, calls = run({**BASE, **CREATE, **DB})
    assert p.returncode != 0 and "re-run with --yes" in (p.stdout + p.stderr)
    assert "put-secret-value" not in calls and not deployed(calls) and "SAM build" not in calls


def test_a_secret_that_already_holds_the_values_is_not_rewritten(run):
    secret = json.dumps({"SQL_SERVER": DB["SQL_SERVER"], "SQL_PORT": DB["SQL_PORT"]})
    p, calls = run({**BASE, **CREATE, "SQL_SERVER": DB["SQL_SERVER"], "SQL_PORT": DB["SQL_PORT"]}, args=["--yes"], secret=secret)
    assert p.returncode == 0 and "nothing to write" in p.stdout and "put-secret-value" not in calls and deployed(calls)


@pytest.mark.parametrize("settings,secret,fail,message", [
    ({"SQL_PORT": "abc"}, None, False, "SQL_PORT must be a whole number"),
    ({"SQL_SERVER": "x", "SQL_CA_PEM_FILE": "/no/such/file.pem"}, None, False, "SQL_CA_PEM_FILE does not exist"),
    ({"SQL_SERVER": "x"}, "[1, 2]", False, "not a JSON object"),
    ({"SQL_SERVER": "x"}, "not json at all", False, "not a JSON object"),
    ({"SQL_SERVER": "x"}, None, True, "could not read the credentials secret")])
def test_a_problem_with_the_secret_or_the_values_stops_before_building_or_deploying(run, settings, secret, fail, message):
    p, calls = run({**BASE, **CREATE, **settings}, args=["--yes"], secret=secret, secret_fail=fail)
    assert p.returncode != 0 and message in (p.stdout + p.stderr)
    assert "put-secret-value" not in calls and "SAM build" not in calls and not deployed(calls)


def test_a_secret_arn_that_is_not_a_secrets_manager_arn_is_refused(run):
    p, calls = run({**BASE, **CREATE, "SYTELINE_SECRET_ARN": "not-an-arn", "SQL_SERVER": "x"}, args=["--yes"])
    assert p.returncode != 0 and "not a Secrets Manager ARN" in (p.stdout + p.stderr) and not deployed(calls)


def test_the_standalone_mode_merges_into_its_secret_too(run):
    p, calls = sa(run, {"SQL_SERVER": DB["SQL_SERVER"], "SQL_DATABASE": "SL", "SQL_USER": "u", "SQL_PASS": "p"}, args=["--yes"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert written(run)["SQL_SERVER"] == DB["SQL_SERVER"] and calls.index("put-secret-value") < calls.index("SAM build")


def test_build_and_validate_modes_never_touch_the_secret(run):
    for flag in ("--build", "--validate"):
        p, calls = run({**BASE, **CREATE, **DB}, args=[flag, "--yes"])
        assert not any(c in calls for c in SECRET_CALLS), flag
