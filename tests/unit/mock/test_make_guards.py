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


def test_prepare_says_how_to_fix_a_python_without_pyyaml(tmp_path):
    r = make(["prepare", "PY=false"], tmp_path)
    assert r.returncode != 0 and "lacks PyYAML or requests" in r.stderr and "requirements.txt" in r.stderr and "PY=" in r.stderr


def test_a_python_with_pyyaml_is_found_when_the_first_one_has_none(tmp_path):
    r = make(["-n", "deps"], tmp_path, {"python3": "exit 1"})      # a python3 that cannot import anything comes first on PATH
    assert r.returncode == 0 and "lacks PyYAML or requests" not in r.stderr


def compose_line(tmp_path, stubs, *extra):
    r = make(["-n", "up", *extra], tmp_path, stubs)
    return next((l for l in r.stdout.splitlines() if "compose.yaml" in l and "up -d" in l), r.stdout + r.stderr)


def test_the_compose_plugin_is_used_when_it_works(tmp_path):
    line = compose_line(tmp_path, {"docker": "exit 0", "docker-compose": "exit 0"})
    assert line.startswith("docker compose -f ")


def test_the_older_docker_compose_is_used_when_the_plugin_is_missing(tmp_path):
    line = compose_line(tmp_path, {"docker": "exit 1", "docker-compose": "exit 0"})
    assert line.startswith("docker-compose -f ")


def test_the_compose_command_can_be_overridden(tmp_path):
    line = compose_line(tmp_path, {"docker": "exit 1"}, "DOCKER_COMPOSE=podman compose")
    assert line.startswith("podman compose -f ")


def test_without_either_the_plugin_command_is_kept_so_its_error_explains_what_is_missing(tmp_path):
    assert compose_line(tmp_path, {"docker": "exit 1"}).startswith("docker compose -f ")


def test_up_stops_early_with_the_fix_when_compose_is_missing(tmp_path):
    r = make(["up"], tmp_path, {"docker": "exit 1"})
    assert r.returncode != 0 and "the Compose tool is missing" in r.stderr and "brew install docker-compose" in r.stderr
    assert "sam build" not in r.stdout          # nothing slow ran first


def test_up_stops_early_when_the_daemon_is_not_running(tmp_path):
    stub = 'case "$1" in compose) exit 0;; info) exit 1;; esac; exit 0'
    r = make(["up"], tmp_path, {"docker": stub})
    assert r.returncode != 0 and "Docker daemon is not running" in r.stderr and "colima start" in r.stderr
