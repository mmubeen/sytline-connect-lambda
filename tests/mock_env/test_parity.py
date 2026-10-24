"""US3: the mock is not easier than production (needs `make -C mock up`)."""

import json
import os
import subprocess

import boto3
import pytest
from botocore.exceptions import ClientError

import parity
from .helpers import COMPOSE_FILE, EDGE, RUN_DIR, compose_command

COMPOSE = [*compose_command(), "-f", COMPOSE_FILE]


def sh(*args):
    return subprocess.run([*COMPOSE, *args], capture_output=True, text=True, timeout=60)


def test_the_run_directory_still_agrees_with_the_template_and_the_code():
    assert parity.check(RUN_DIR) == []
    assert parity.undocumented_actions() == []


def test_the_containers_received_the_same_environment_as_the_template_renders(only_mock_addresses):
    wanted = only_mock_addresses
    got = dict(line.split("=", 1) for line in sh("exec", "-T", "fn1", "printenv").stdout.splitlines() if "=" in line)
    assert {k: got.get(k) for k in wanted} == wanted


def test_timeout_and_memory_are_the_templates():
    rendered = json.load(open(os.path.join(RUN_DIR, "rendered.json")))
    cid = sh("ps", "-q", "fn1").stdout.strip()
    inspect = json.loads(subprocess.run(["docker", "inspect", cid], capture_output=True, text=True).stdout)[0]
    assert inspect["HostConfig"]["Memory"] == rendered["memory"] * 1024 * 1024
    assert f"AWS_LAMBDA_FUNCTION_TIMEOUT={rendered['timeout']}" in inspect["Config"]["Env"]


def test_a_call_outside_the_templates_permissions_is_denied_like_aws(control):
    s3 = boto3.client("s3", endpoint_url=EDGE, region_name="us-gov-east-1", aws_access_key_id="mock",
                      aws_secret_access_key="mock")
    with pytest.raises(ClientError) as e:
        s3.put_object(Bucket="mock-idp-output", Key="x", Body=b"x")
    assert e.value.response["Error"]["Code"] == "AccessDenied" and "s3:PutObject" in str(e.value)


def test_api_routes_reach_their_handlers_and_unknown_routes_get_404(control):
    import requests

    def api(method, path, **kw):
        return requests.post(EDGE + "/__mock__/invoke/api", json={"method": method, "path": path, **kw}, timeout=60).json()

    ok = api("GET", "/upload-jobs/abc")
    assert ok["payload"]["statusCode"] in (200, 404) and "body" in ok["payload"]
    assert api("GET", "/no-such-route")["payload"]["statusCode"] == 404
