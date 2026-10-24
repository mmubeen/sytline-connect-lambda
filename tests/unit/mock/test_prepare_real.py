"""T056: real-IDP mode in `edge.prepare` (the developer's profile is exercised through a stub `aws`)."""

import json
import os
import stat
import subprocess
import time

import pytest

from edge import prepare

SECRET, TOKEN, KEY = "devSecretValue123", "devSessionToken456", "ASIADEVKEY789"
SETTINGS = {"MOCK_IDP": "real", "MOCK_IDP_PROFILE": "idp-dev", "MOCK_IDP_STACK": "us-ct-idp-stack",
            "MOCK_IDP_BUCKET": "us-ct-idp-output", "MOCK_IDP_TABLE": "us-ct-idp-tracking", "MOCK_IDP_DOCUMENT": "159112.pdf"}


def aws(expires=None, fail=None):
    expires = expires if expires is not None else time.time() + 3600
    calls = []

    def run(args, **kw):
        calls.append(args)
        if fail and fail in " ".join(args):
            return subprocess.CompletedProcess(args, 255, "", "Error loading SSO Token: Token for idp-dev does not exist")
        if "get-caller-identity" in args:
            return subprocess.CompletedProcess(args, 0, "123456789012\n", "")
        out = (f"AWS_ACCESS_KEY_ID={KEY}\nAWS_SECRET_ACCESS_KEY={SECRET}\nAWS_SESSION_TOKEN={TOKEN}\n"
               f"AWS_CREDENTIAL_EXPIRATION={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(expires))}\n")
        return subprocess.CompletedProcess(args, 0, out, "")

    run.calls = calls
    return run


@pytest.fixture
def dirs(tmp_path):
    return str(tmp_path / "run"), str(tmp_path / "idp")


def go(dirs, env=None, runner=None):
    return prepare.prepare(dirs[0], idp_dir=dirs[1], environ={**SETTINGS, **(env or {})}, runner=runner or aws())


def test_credentials_and_mode_are_written_privately(dirs):
    go(dirs)
    creds = os.path.join(dirs[1], "creds")
    assert stat.S_IMODE(os.stat(creds).st_mode) == 0o600
    assert f"AWS_SESSION_TOKEN={TOKEN}" in open(creds).read()
    mode = json.load(open(os.path.join(dirs[1], "mode.json")))
    assert mode["mode"] == "real" and mode["document"] == "159112.pdf" and mode["account"] == "123456789012"
    assert mode["region"] == "us-gov-east-1" and mode["partition"] == "aws-us-gov" and mode["fips"] is True
    assert SECRET not in json.dumps(mode) and TOKEN not in json.dumps(mode)


def test_nothing_secret_is_in_the_function_environment_or_the_rendered_settings(dirs):
    go(dirs)
    for name in ("function.env", "rendered.json", "sql.env", "passwords.json"):
        text = open(os.path.join(dirs[0], name)).read()
        assert SECRET not in text and TOKEN not in text and KEY not in text


def test_the_real_identifiers_and_account_reach_the_settings(dirs):
    go(dirs)
    env = dict(line.split("=", 1) for line in open(os.path.join(dirs[0], "function.env")).read().splitlines())
    assert env["IDP_STACK_NAME"] == "us-ct-idp-stack" and env["IDP_REGION"] == "us-gov-east-1"
    rendered = json.load(open(os.path.join(dirs[0], "rendered.json")))
    assert any("123456789012:function:us-ct-idp-stack-LookupFunction-*" in r for s in rendered["statements"] for r in s["Resource"])
    assert env["AWS_ENDPOINT_URL"] == "http://edge:4566"      # the function still talks only to the edge


@pytest.mark.parametrize("missing", ["MOCK_IDP_PROFILE", "MOCK_IDP_STACK", "MOCK_IDP_BUCKET", "MOCK_IDP_TABLE", "MOCK_IDP_DOCUMENT"])
def test_each_missing_setting_is_named(dirs, missing):
    env = {k: v for k, v in SETTINGS.items() if k != missing}
    with pytest.raises(SystemExit) as e:
        prepare.prepare(dirs[0], idp_dir=dirs[1], environ=env, runner=aws())
    assert missing in str(e.value)


def test_a_profile_that_cannot_export_credentials_is_refused_with_the_cause(dirs):
    with pytest.raises(SystemExit) as e:
        go(dirs, runner=aws(fail="export-credentials"))
    assert "idp-dev" in str(e.value) and "SSO" in str(e.value)
    assert not os.path.exists(os.path.join(dirs[1], "creds"))


def test_already_expired_credentials_are_refused(dirs):
    with pytest.raises(SystemExit) as e:
        go(dirs, runner=aws(expires=time.time() - 5))
    assert "expired" in str(e.value)


def test_an_invalid_document_id_is_refused(dirs):
    with pytest.raises(SystemExit) as e:
        go(dirs, env={"MOCK_IDP_DOCUMENT": "../etc/passwd"})
    assert "MOCK_IDP_DOCUMENT" in str(e.value)


def test_the_default_is_mock_mode_with_no_credentials_kept(dirs):
    os.makedirs(dirs[1])
    open(os.path.join(dirs[1], "creds"), "w").write("stale")
    prepare.prepare(dirs[0], idp_dir=dirs[1], environ={}, runner=aws())
    assert json.load(open(os.path.join(dirs[1], "mode.json"))) == {"mode": "mock"}
    assert not os.path.exists(os.path.join(dirs[1], "creds"))


def test_refreshing_only_replaces_the_credentials(dirs):
    go(dirs)
    first = open(os.path.join(dirs[1], "mode.json")).read()
    prepare.refresh_credentials(idp_dir=dirs[1], runner=aws(expires=time.time() + 7200))
    assert open(os.path.join(dirs[1], "mode.json")).read() == first
    assert f"AWS_ACCESS_KEY_ID={KEY}" in open(os.path.join(dirs[1], "creds")).read()


def test_fips_can_be_turned_off(dirs):
    go(dirs, env={"MOCK_IDP_FIPS": "false"})
    assert json.load(open(os.path.join(dirs[1], "mode.json")))["fips"] is False
