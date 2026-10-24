"""T038: the mock Makefile's guards (no Docker needed: they fire before anything is started)."""

import os
import stat
import subprocess

MOCK = os.path.join(os.path.dirname(__file__), "..", "..", "..", "mock")


def make(args, tmp_path, stubs=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (stubs or {}).items():
        f = bin_dir / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run(["make", "-C", MOCK, f"RUN={tmp_path / 'run'}", *args], capture_output=True, text=True, env=env)


def test_a_second_test_run_is_refused_with_the_lock_named(tmp_path):
    (tmp_path / "run" / "test.lock").mkdir(parents=True)
    r = make(["test"], tmp_path)
    assert r.returncode != 0 and "another test run holds" in r.stderr and "test.lock" in r.stderr  # make exits 2; the recipe's own code is 3


def test_the_lock_is_released_when_the_run_ends(tmp_path):
    r = make(["test"], tmp_path, {"curl": "exit 1", "docker": "exit 1", "sam": "exit 1"})  # `up` fails: no Docker
    assert not (tmp_path / "run" / "test.lock").exists() and r.returncode != 0


def test_wait_names_the_service_that_was_not_ready(tmp_path):
    body = 'echo \'{"ready": false, "services": {"moto": "up", "sql": "starting"}}\''
    r = make(["wait", "WAIT=1", "POLL=1"], tmp_path, {"curl": body})
    assert r.returncode != 0 and "not ready after 1s" in r.stderr and '"sql": "starting"' in r.stderr


def test_wait_succeeds_when_the_environment_reports_ready(tmp_path):
    r = make(["wait", "WAIT=5", "POLL=1"], tmp_path, {"curl": 'echo \'{"ready": true, "services": {}}\''})
    assert r.returncode == 0 and "ready" in r.stdout


def test_build_needs_the_idp_checkout(tmp_path):
    r = make(["build"], tmp_path)
    assert r.returncode != 0 and "IDP_REPO" in r.stderr
