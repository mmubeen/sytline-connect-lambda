"""scr/deploy.sh behavior with stubbed `sam` and `aws` (contracts/template-interface.md)."""

import os
import re
import shutil
import stat
import subprocess

import pytest

SCR = os.path.join(os.path.dirname(__file__), "..", "..", "scr")

BASE = {"SYTELINE_WSDL": "http://erp/IDORequestService/IDOWebService.asmx?wsdl", "SYTELINE_USER": "svc",
        "SYTELINE_SECRET_ARN": "arn:aws-us-gov:secretsmanager:us-gov-east-1:1:secret:x"}
SUPPLY = {"WORKER_VPC_ID": "vpc-1", "WORKER_SUBNET_IDS": "subnet-a,subnet-b", "WORKER_SG_IDS": "sg-1",
          "WORKER_ROUTE_TABLE_IDS": "rtb-1", "API_VPCE_ID": "vpce-1"}
CREATE = {"HOST_NETWORK_CIDR": "10.50.0.0/24", "PLANT_CIDR_1": "10.9.0.0/16", "API_VPCE_ID": "vpce-1"}

SAM_STUB = '#!/usr/bin/env bash\necho "SAM $*" >> "$STUB_LOG"\n'
AWS_STUB = '''#!/usr/bin/env bash
echo "AWS $*" >> "$STUB_LOG"
case "$*" in
  *list-profiles*) [[ "${STUB_PROFILES_FAIL:-}" == "1" ]] && exit 252; printf '%s\n' ${STUB_PROFILES:-default} ;;
  *StackStatus*) [[ -n "${STUB_STACK_STATUS:-}" ]] && echo "$STUB_STACK_STATUS" || exit 255 ;;
  *HostVpcId*) echo "${STUB_HOSTVPC:-None}" ;;
esac
'''


@pytest.fixture
def run(tmp_path):
    work = tmp_path / "scr"
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

    def go(settings, args=(), stack_status="", host_vpc="", with_aws=True, profiles="default govcloud-prod",
           profiles_fail=False):
        (work / "deploy.env").write_text("".join(f'{k}="{v}"\n' for k, v in settings.items()))
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(log), "STUB_STACK_STATUS": stack_status,
               "STUB_HOSTVPC": host_vpc, "STUB_PROFILES": profiles, "STUB_PROFILES_FAIL": "1" if profiles_fail else ""}
        if not with_aws:
            (bin_dir / "aws").unlink()
        log.write_text("")
        p = subprocess.run(["bash", str(work / "deploy.sh"), *args], capture_output=True, text=True, env=env, cwd=work)
        calls = log.read_text()
        return p, calls

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
