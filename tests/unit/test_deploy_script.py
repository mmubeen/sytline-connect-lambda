"""src/deploy.sh behavior with stubbed `sam` and `aws` (specs/007: one deployment, an existing network)."""

import os
import re
import shutil
import stat
import subprocess

import pytest

SCR = os.path.join(os.path.dirname(__file__), "..", "..", "src")

BASE = {"SYTELINE_WSDL": "http://erp/IDORequestService/IDOWebService.asmx?wsdl", "SYTELINE_USER": "svc",
        "SYTELINE_SECRET_ARN": "arn:aws-us-gov:secretsmanager:us-gov-east-1:1:secret:x"}
# the existing network: all four are required, nothing is created
NET = {"WORKER_VPC_ID": "vpc-0abc1234", "WORKER_SUBNET_IDS": "subnet-0a1,subnet-0b2", "WORKER_SG_IDS": "sg-0c3",
       "API_VPCE_ID": "vpce-0d4"}
SUPPLY = CREATE = NET  # the earlier names of this module's helpers

SAM_STUB = '#!/usr/bin/env bash\necho "SAM $*" >> "$STUB_LOG"\n'
AWS_STUB = '''#!/usr/bin/env bash
echo "AWS $*" >> "$STUB_LOG"
case "$*" in
  *get-secret-value*) [[ "${STUB_SECRET_FAIL:-}" == "1" ]] && exit 254; printf '%s' "$STUB_SECRET" ;;
  *put-secret-value*) f="$(printf '%s\n' "$@" | sed -n 's#^file://##p')"; cp "$f" "$STUB_PUT"; echo "tmpfile-mode $(stat -f %Lp "$f" 2>/dev/null || stat -c %a "$f")" >> "$STUB_LOG" ;;
  *list-profiles*) [[ "${STUB_PROFILES_FAIL:-}" == "1" ]] && exit 252; printf '%s\n' ${STUB_PROFILES:-default} ;;
  *list-stack-resources*) printf '%s' "${STUB_OLD_FUNCTIONS:-None}" ;;
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

    def go(settings, args=(), old_functions="", with_aws=True, profiles="default govcloud-prod",
           profiles_fail=False, env_name="deploy.env", write_env=True, secret=None, secret_fail=False):
        if write_env:
            (work / env_name).write_text("".join(f'{k}="{v}"\n' for k, v in settings.items()))
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(log), "STUB_OLD_FUNCTIONS": old_functions or "None",
               "STUB_PROFILES": profiles, "STUB_PROFILES_FAIL": "1" if profiles_fail else "",
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




# ---- route settings (feature 005) -------------------------------------------------------------
ROUTE_KEYS = ("NextCoIdo", "NextJobIdo", "CustomerIdo", "DefaultsIdo")


def test_the_route_parameters_default_to_the_database_and_are_always_passed(run):
    p, calls = run({**BASE, **NET})
    assert p.returncode == 0, p.stderr
    o = overrides(calls)
    assert {k: o[k] for k in ROUTE_KEYS} == {k: "database" for k in ROUTE_KEYS}  # always given, so an old value is cleared


def test_an_endpoint_name_in_the_settings_keeps_that_operation_on_the_web_service(run):
    p, calls = run({**BASE, **NET, "NEXT_CO_IDO": "SLCos", "CUSTOMER_IDO": "SLCustAddrs"})
    o = overrides(calls)
    assert (o["NextCoIdo"], o["NextJobIdo"], o["CustomerIdo"], o["DefaultsIdo"]) == ("SLCos", "database", "SLCustAddrs",
                                                                                     "database")


def test_sql_encrypt_defaults_to_true_is_always_passed_and_false_warns(run):
    p, calls = run({**BASE, **NET})
    assert overrides(calls)["SqlEncrypt"] == "true" and "UNENCRYPTED" not in p.stdout
    p, calls = run({**BASE, **NET, "SQL_ENCRYPT": "false"})
    assert p.returncode == 0 and overrides(calls)["SqlEncrypt"] == "false" and "UNENCRYPTED" in p.stdout


def test_sql_encrypt_must_be_true_or_false_and_nothing_is_deployed_otherwise(run):
    p, calls = run({**BASE, **NET, "SQL_ENCRYPT": "maybe"})
    assert p.returncode != 0 and "SQL_ENCRYPT must be true or false" in (p.stdout + p.stderr) and not deployed(calls)


TUNING = {"SqlVerifyHostname": "true", "SqlLoginTimeout": "5", "SqlTimeout": "10", "SiteId": "10", "MatchMinScore": "70",
          "MatchMargin": "3", "MatchCap": "1000", "UseFipsEndpoints": "true", "IdpDocumentClass": "", "IdpFieldMap": ""}


def test_the_tuning_parameters_are_always_passed_with_the_codes_own_defaults(run):
    p, calls = run({**BASE, **NET})
    assert p.returncode == 0, p.stderr
    o = overrides(calls)
    assert {k: o[k] for k in TUNING} == TUNING  # always given, so an earlier value is cleared


def test_the_tuning_settings_are_passed_through(run):
    p, calls = run({**BASE, **NET, "SQL_VERIFY_HOSTNAME": "false", "SQL_LOGIN_TIMEOUT": "8", "SQL_TIMEOUT": "20",
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
    p, calls = run({**BASE, **NET, name: value})
    assert p.returncode != 0 and message in (p.stdout + p.stderr) and not deployed(calls)


def test_the_idp_field_map_json_keeps_its_quotes_through_sams_parser(run):
    p, calls = run({**BASE, **NET, "IDP_FIELD_MAP": '{\\"po_number\\":\\"purchase_order\\"}'})
    assert p.returncode == 0, p.stdout + p.stderr
    line = [l for l in calls.splitlines() if l.startswith("SAM deploy")][0]
    assert 'IdpFieldMap="{\\"po_number\\":\\"purchase_order\\"}"' in line


def test_an_idp_field_map_that_is_not_a_json_object_is_refused(run):
    p, calls = run({**BASE, **NET, "IDP_FIELD_MAP": "{po_number:purchase_order}"})
    assert p.returncode != 0 and "IDP_FIELD_MAP must be a JSON object" in (p.stdout + p.stderr) and not deployed(calls)



# ---- unchanged feature-001 behavior ------------------------------------------------------
def test_live_writes_still_need_an_approval_reference(run):
    p, calls = run({**BASE, **NET, "LIVE_WRITES": "true"})
    assert p.returncode != 0 and "LIVE_APPROVAL_REF" in p.stderr and not deployed(calls)
    p, calls = run({**BASE, **NET, "LIVE_WRITES": "true", "LIVE_APPROVAL_REF": "ERP-OWNER-123"})
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
                else "stack lookup" if "list-stack-resources" in line else None)
        if step:
            m = re.search(r"--profile (\S+)", line)
            out.setdefault(step, set()).add(m.group(1) if m else None)
    return out


def test_no_profile_means_no_profile_flag_anywhere(run):
    p, calls = run({**BASE, **NET})
    assert p.returncode == 0 and "--profile" not in calls and "Using AWS profile" not in p.stdout


def test_profile_flag_reaches_every_aws_facing_step(run):
    p, calls = run({**BASE, **NET}, args=["--profile", "govcloud-prod"])
    assert p.returncode == 0 and "Using AWS profile: govcloud-prod" in p.stdout
    seen = profile_args_of(calls)
    assert seen == {"build": {"govcloud-prod"}, "deploy": {"govcloud-prod"}, "stack lookup": {"govcloud-prod"}}
    assert calls.count("--profile govcloud-prod") >= 3  # build, deploy and the stack look-up


def test_equals_form_and_validate_and_build_modes(run):
    p, calls = run({**BASE, **NET}, args=["--profile=govcloud-prod", "--validate"])
    assert p.returncode == 0 and "SAM validate --lint --profile govcloud-prod" in calls
    p, calls = run({**BASE, **NET}, args=["--build", "--profile", "govcloud-prod"])
    assert p.returncode == 0 and "SAM build --profile govcloud-prod" in calls and not deployed(calls)


def test_profile_from_deploy_env_and_flag_wins(run):
    p, calls = run({**BASE, **NET, "AWS_PROFILE_NAME": "govcloud-prod"})
    assert p.returncode == 0 and "SAM deploy --profile govcloud-prod" in calls
    p, calls = run({**BASE, **NET, "AWS_PROFILE_NAME": "govcloud-prod"}, args=["--profile", "default"])
    assert p.returncode == 0 and "--profile default" in calls and "govcloud-prod" not in calls


def test_unknown_profile_is_refused_before_anything_runs(run):
    p, calls = run({**BASE, **NET}, args=["--profile", "nope"])
    assert p.returncode != 0 and "'nope' is not configured" in p.stderr
    assert "SAM" not in calls and "describe-stacks" not in calls


@pytest.mark.parametrize("bad", ["a b", "x;rm -rf", "$(id)", "p/q"])
def test_invalid_profile_names_are_refused(run, bad):
    p, calls = run({**BASE, **NET}, args=["--profile", bad])
    assert p.returncode != 0 and "invalid profile name" in p.stderr and "SAM" not in calls


@pytest.mark.parametrize("args", [["--profile"], ["--profile="], ["--profile", "--validate"]])
def test_profile_without_a_name_is_refused(run, args):
    p, calls = run({**BASE, **NET}, args=args)
    assert p.returncode != 0 and "needs a profile name" in p.stderr and "SAM" not in calls


def test_profile_check_is_skipped_with_a_warning_if_profiles_cannot_be_listed(run):
    p, calls = run({**BASE, **NET}, args=["--profile", "whatever"], profiles_fail=True)
    assert p.returncode == 0 and "could not list AWS profiles" in p.stdout and "--profile whatever" in calls


def test_profile_works_without_the_aws_cli_installed(run):
    p, calls = run({**BASE, **NET}, args=["--profile", "govcloud-prod"], with_aws=False)
    assert p.returncode == 0 and "SAM deploy --profile govcloud-prod" in calls


# ---- the database settings are merged into the credentials secret (not passed to CloudFormation) ---------------------
import json  # noqa: E402

DB = {"SQL_SERVER": "sql.plant.example", "SQL_DATABASE": "SL_PROD", "SQL_USER": "svc-login", "SQL_PASS": "S3cr3t-DB-Pass",
      "SQL_PORT": "14330"}
SECRET_CALLS = ("get-secret-value", "put-secret-value")


def written(run):
    return json.loads(run.put_path.read_text())


def test_nothing_is_read_or_written_when_no_database_setting_is_given(run):
    p, calls = run({**BASE, **NET})
    assert p.returncode == 0 and not any(c in calls for c in SECRET_CALLS) and deployed(calls)


def test_the_database_values_are_merged_into_the_secret_keeping_its_other_keys(run):
    pem = run.work / "ca.pem"
    pem.write_text("-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n")
    p, calls = run({**BASE, **NET, **DB, "SQL_CA_PEM_FILE": str(pem)}, args=["--yes"],
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
    p, calls = run({**BASE, **NET, "SQL_SERVER": "new.plant.example"}, args=["--yes"],
                   secret='{"SQL_SERVER": "old", "SQL_USER": "keep-me", "SQL_PASS": "keep-pass"}')
    assert p.returncode == 0
    assert written(run) == {"SQL_SERVER": "new.plant.example", "SQL_USER": "keep-me", "SQL_PASS": "keep-pass"}


def test_the_values_never_reach_the_output_the_command_lines_or_the_stack_parameters(run):
    p, calls = run({**BASE, **NET, **DB}, args=["--yes"])
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
    p, calls = run({**BASE, **NET, **DB}, args=["--yes"])
    assert "tmpfile-mode 600" in calls
    path = [a for a in calls.split() if a.startswith("file://")][0][len("file://"):]
    assert not os.path.exists(path)


def test_without_yes_and_without_a_terminal_it_refuses_and_changes_nothing(run):
    p, calls = run({**BASE, **NET, **DB})
    assert p.returncode != 0 and "re-run with --yes" in (p.stdout + p.stderr)
    assert "put-secret-value" not in calls and not deployed(calls) and "SAM build" not in calls


def test_a_secret_that_already_holds_the_values_is_not_rewritten(run):
    secret = json.dumps({"SQL_SERVER": DB["SQL_SERVER"], "SQL_PORT": DB["SQL_PORT"]})
    p, calls = run({**BASE, **NET, "SQL_SERVER": DB["SQL_SERVER"], "SQL_PORT": DB["SQL_PORT"]}, args=["--yes"], secret=secret)
    assert p.returncode == 0 and "nothing to write" in p.stdout and "put-secret-value" not in calls and deployed(calls)


@pytest.mark.parametrize("settings,secret,fail,message", [
    ({"SQL_PORT": "abc"}, None, False, "SQL_PORT must be a whole number"),
    ({"SQL_SERVER": "x", "SQL_CA_PEM_FILE": "/no/such/file.pem"}, None, False, "SQL_CA_PEM_FILE does not exist"),
    ({"SQL_SERVER": "x"}, "[1, 2]", False, "not a JSON object"),
    ({"SQL_SERVER": "x"}, "not json at all", False, "not a JSON object"),
    ({"SQL_SERVER": "x"}, None, True, "could not read the credentials secret")])
def test_a_problem_with_the_secret_or_the_values_stops_before_building_or_deploying(run, settings, secret, fail, message):
    p, calls = run({**BASE, **NET, **settings}, args=["--yes"], secret=secret, secret_fail=fail)
    assert p.returncode != 0 and message in (p.stdout + p.stderr)
    assert "put-secret-value" not in calls and "SAM build" not in calls and not deployed(calls)


def test_a_secret_arn_that_is_not_a_secrets_manager_arn_is_refused(run):
    p, calls = run({**BASE, **NET, "SYTELINE_SECRET_ARN": "not-an-arn", "SQL_SERVER": "x"}, args=["--yes"])
    assert p.returncode != 0 and "not a Secrets Manager ARN" in (p.stdout + p.stderr) and not deployed(calls)


def test_build_and_validate_modes_never_touch_the_secret(run):
    for flag in ("--build", "--validate"):
        p, calls = run({**BASE, **NET, **DB}, args=[flag, "--yes"])
        assert not any(c in calls for c in SECRET_CALLS), flag



def test_the_merge_notice_names_the_keys_and_never_a_value(run):
    p, calls = run({**BASE, **NET, **DB}, args=["--yes"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert ("Will add or update these keys in the credentials secret: SQL_SERVER SQL_DATABASE SQL_USER SQL_PASS SQL_PORT."
            in p.stdout)
    assert "All other keys stay as they are, and values are never printed." in p.stdout
    assert "MERGED" not in p.stdout
    for value in DB.values():
        assert value not in p.stdout + p.stderr



@pytest.mark.parametrize("arn", [
    "arn:aws-us-gov:secretsmanager:us-gov-east-1:ACCOUNT:secret:NAME",      # the example's placeholder
    "arn:aws-us-gov:secretsmanager:us-gov-east-1:123456789012:secret:x y",  # a space
    "arn:aws-us-gov:secretsmanager:us-gov-east-1:123456789012:secret:x\r",  # a carriage return
    "arn:aws-us-gov:secretsmanager:us-gov-east-1:123456789012",             # cut off
])
def test_an_incomplete_secret_arn_is_named_before_aws_is_called(run, arn):
    p, calls = run({**BASE, **NET, "SYTELINE_SECRET_ARN": arn, "SQL_SERVER": "x"}, args=["--yes"])
    assert p.returncode != 0 and "not a complete secret ARN" in p.stderr and "ACCOUNT placeholder" in p.stderr
    assert "get-secret-value" not in calls and not deployed(calls)


# ---- the event intake rule (specs/006): IDP_STATE_MACHINE_ARN -----------------------------------------------------------
SM_ARN = "arn:aws-us-gov:states:us-gov-east-1:108375521309:stateMachine:idp-DocumentProcessingWorkflow"
IDP = {"IDP_STACK_NAME": "idp", "IDP_OUTPUT_BUCKET": "out-bucket", "IDP_TRACKING_TABLE": "tracking"}


def test_the_state_machine_arn_is_always_passed_and_empty_clears_an_earlier_value(run):
    p, calls = run({**BASE, **NET})
    assert p.returncode == 0, p.stderr
    assert overrides(calls)["IdpStateMachineArn"] == ""
    p, calls = run({**BASE, **NET, **IDP, "IDP_STATE_MACHINE_ARN": SM_ARN})
    assert p.returncode == 0, p.stderr
    assert overrides(calls)["IdpStateMachineArn"] == SM_ARN


@pytest.mark.parametrize("bad", ["not-an-arn", SM_ARN + " x", SM_ARN.replace("stateMachine", "execution")])
def test_a_malformed_state_machine_arn_is_refused_before_any_sam_call(run, bad):
    p, calls = run({**BASE, **NET, **IDP, "IDP_STATE_MACHINE_ARN": bad})
    assert p.returncode != 0 and "IDP_STATE_MACHINE_ARN is not a Step Functions state machine ARN" in p.stderr
    assert "SAM" not in calls


def test_the_state_machine_arn_needs_the_idp_settings(run):
    p, calls = run({**BASE, **NET, "IDP_STATE_MACHINE_ARN": SM_ARN})
    assert p.returncode != 0 and "needs IDP_STACK_NAME" in p.stderr and "SAM" not in calls


# ---- one deployment, an existing network (specs/007) -----------------------------------------------------------------
@pytest.mark.parametrize("missing", ["WORKER_VPC_ID", "WORKER_SUBNET_IDS", "WORKER_SG_IDS"])
def test_each_network_setting_is_required_and_a_missing_one_stops_before_any_sam_call(run, missing):
    settings = {**BASE, **{k: v for k, v in NET.items() if k != missing}}
    p, calls = run(settings)
    assert p.returncode != 0 and missing in p.stderr and "SAM" not in calls


@pytest.mark.parametrize("name,value", [
    ("WORKER_VPC_ID", "subnet-0a1"), ("WORKER_VPC_ID", "vpc-"), ("WORKER_SUBNET_IDS", "subnet-0a1, subnet-0b2"),
    ("WORKER_SUBNET_IDS", "sg-1"), ("WORKER_SG_IDS", "subnet-1"), ("WORKER_SG_IDS", "sg-1;rm"),
    ("API_VPCE_ID", "vpc-1"), ("API_VPCE_ID", "vpce-")])
def test_a_malformed_network_identifier_is_refused_before_any_sam_call(run, name, value):
    p, calls = run({**BASE, **NET, name: value})
    assert p.returncode != 0 and name in p.stderr and "SAM" not in calls


def test_the_four_network_values_are_passed_and_nothing_else_network_shaped(run):
    p, calls = run({**BASE, **NET})
    assert p.returncode == 0, p.stderr
    o = overrides(calls)
    assert o["WorkerVpcId"] == NET["WORKER_VPC_ID"] and o["WorkerSubnetIds"] == NET["WORKER_SUBNET_IDS"]
    assert o["WorkerSecurityGroupIds"] == NET["WORKER_SG_IDS"] and o["ApiVpcEndpointId"] == NET["API_VPCE_ID"]
    for gone in ("WorkerRouteTableIds", "HostNetworkCidr", "PlantCidr1", "DynamoDbPrefixListId", "CreateVpcEndpoints",
                 "CreateApiEndpoint", "AllowPlainHttp", "VerifyTls", "AttachVpcId", "DatabaseCheck"):
        assert gone not in o
    assert "nothing network-related is created" in p.stdout and "Reminder: this network must give" in p.stdout


def test_a_single_subnet_is_accepted_with_a_high_availability_warning(run):
    p, calls = run({**BASE, **NET, "WORKER_SUBNET_IDS": "subnet-0a1"})
    assert p.returncode == 0 and "only one subnet" in p.stdout and overrides(calls)["WorkerSubnetIds"] == "subnet-0a1"
    p, _ = run({**BASE, **NET})
    assert "only one subnet" not in p.stdout


@pytest.mark.parametrize("name", ["HOST_NETWORK_CIDR", "PLANT_CIDR_1", "PLANT_CIDR_2", "PLANT_CIDR_3", "DYNAMODB_PREFIX_LIST_ID",
                                  "CREATE_VPC_ENDPOINTS", "CREATE_API_ENDPOINT", "WORKER_ROUTE_TABLE_IDS", "ATTACH_VPC_ID",
                                  "ATTACH_SUBNET_IDS", "ATTACH_SG_IDS", "ALLOW_PLAIN_HTTP", "VERIFY_TLS", "DATABASE_CHECK"])
def test_a_setting_of_the_earlier_shapes_gets_a_notice_and_is_not_passed(run, name):
    p, calls = run({**BASE, **NET, name: "x"})
    assert p.returncode == 0, p.stderr
    assert "no longer used by this deployment" in p.stdout and name in p.stdout
    assert not any(name.replace("_", "").lower() == k.lower() for k in overrides(calls))


def test_there_is_no_network_mode_flag(run):
    p, calls = run({**BASE, **NET}, args=["--allow-network-change"])
    assert p.returncode == 2 and "unknown argument" in p.stdout and "SAM" not in calls


def test_the_notice_about_functions_of_the_earlier_shape_appears_only_when_they_exist(run):
    p, calls = run({**BASE, **NET}, old_functions="sytline-connect-ReserveFunction-AB12\tsytline-connect-WorkerFunction-CD34")
    assert p.returncode == 0 and "still has functions of the earlier shape" in p.stdout
    assert "sytline-connect-ReserveFunction-AB12" in p.stdout and "docs/MIGRATION.md" in p.stdout
    assert "list-stack-resources" in calls and "sytline-connect-fn" in calls  # the one function itself is excluded by name
    p, _ = run({**BASE, **NET})
    assert "earlier shape" not in p.stdout
    p, _ = run({**BASE, **NET}, with_aws=False)
    assert p.returncode == 0 and "earlier shape" not in p.stdout


def test_the_api_endpoint_is_optional_and_without_it_no_api_is_requested(run):
    settings = {**BASE, **{k: v for k, v in NET.items() if k != "API_VPCE_ID"}}
    p, calls = run(settings)
    assert p.returncode == 0, p.stderr
    assert overrides(calls)["ApiVpcEndpointId"] == ""  # always passed: empty = no API, and it clears an earlier endpoint
    assert "NO API is created" in p.stdout and "private API will be created" not in p.stdout
    p, calls = run({**BASE, **NET})
    assert overrides(calls)["ApiVpcEndpointId"] == NET["API_VPCE_ID"] and "private API will be created" in p.stdout
    assert "NO API is created" not in p.stdout


# ---- the IDP's KMS keys (IDP_KMS_KEY_ARNS) -----------------------------------------------------------------------------
KMS_A = "arn:aws-us-gov:kms:us-gov-east-1:108375521309:key/1234abcd-12ab-34cd-56ef-1234567890ab"
KMS_B = "arn:aws-us-gov:kms:us-gov-east-1:108375521309:key/aaaabbbb-12ab-34cd-56ef-1234567890ab"


def test_the_kms_keys_are_always_passed_and_empty_clears_an_earlier_value(run):
    p, calls = run({**BASE, **NET})
    assert p.returncode == 0 and overrides(calls)["IdpKmsKeyArns"] == ""
    p, calls = run({**BASE, **NET, "IDP_KMS_KEY_ARNS": KMS_A + "," + KMS_B})
    assert p.returncode == 0, p.stderr
    assert overrides(calls)["IdpKmsKeyArns"] == KMS_A + "," + KMS_B


@pytest.mark.parametrize("bad", ["not-an-arn", KMS_A + ", " + KMS_B, "arn:aws-us-gov:kms:us-gov-east-1:1:alias/x", KMS_A + ",x"])
def test_a_malformed_kms_key_arn_is_refused_before_any_sam_call(run, bad):
    p, calls = run({**BASE, **NET, "IDP_KMS_KEY_ARNS": bad})
    assert p.returncode != 0 and "IDP_KMS_KEY_ARNS" in p.stderr and "is not a KMS key ARN" in p.stderr and "SAM" not in calls



# ---- feature 006, FR-016: LOG_EVENTS ----------------------------------------------------------------------------------
def test_log_events_defaults_to_false_and_is_always_passed(run):
    p, calls = run({**BASE, **NET})
    assert p.returncode == 0 and overrides(calls)["LogEvents"] == "false" and "document and order content" not in p.stdout


def test_log_events_true_is_passed_with_a_warning_about_what_the_logs_will_hold(run):
    p, calls = run({**BASE, **NET, "LOG_EVENTS": "true"})
    assert p.returncode == 0 and overrides(calls)["LogEvents"] == "true"
    assert "LOG_EVENTS=true" in p.stdout and "document and order content" in p.stdout and "turn it off" in p.stdout.lower()


def test_log_events_must_be_true_or_false(run):
    p, calls = run({**BASE, **NET, "LOG_EVENTS": "maybe"})
    assert p.returncode != 0 and "LOG_EVENTS must be true or false" in (p.stdout + p.stderr) and not deployed(calls)


# ---- JOB_NUMBER: a fixed job number for tests ---------------------------------------------------------------------------
def test_job_number_defaults_to_empty_and_is_always_passed(run):
    p, calls = run({**BASE, **NET})
    assert p.returncode == 0 and overrides(calls)["JobNumber"] == "" and "JOB_NUMBER=" not in p.stdout


def test_job_number_is_passed_with_a_warning(run):
    p, calls = run({**BASE, **NET, "JOB_NUMBER": "TEST-001"})
    assert p.returncode == 0 and overrides(calls)["JobNumber"] == "TEST-001"
    assert "JOB_NUMBER=TEST-001" in p.stdout and "no new one is allocated" in p.stdout


def test_job_number_must_be_a_plain_identifier(run):
    p, calls = run({**BASE, **NET, "JOB_NUMBER": "a b"})
    assert p.returncode != 0 and "JOB_NUMBER must be empty or" in (p.stdout + p.stderr) and not deployed(calls)
