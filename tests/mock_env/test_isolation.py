"""US4: repeatable, and nothing leaves the machine (needs `make -C mock up`)."""

import json
import os
import subprocess

from .helpers import COMPOSE_FILE, RUN_DIR, compose_command, erp_saves, wait_for

COMPOSE = [*compose_command(), "-f", COMPOSE_FILE]


def one_run(control):
    control.reset()
    event = control.scenario("passes-review")["event"]
    out = control.invoke(event)["payload"]
    wait_for(lambda: erp_saves(control), timeout=90)
    ops = [(c["service"], c["operation"]) for c in control.calls() if c["service"] == "erp"]
    return out["outcome"], ops


def test_two_runs_from_a_reset_state_are_identical(control):
    assert one_run(control) == one_run(control)


def test_the_function_container_cannot_reach_the_outside(control):
    r = subprocess.run([*COMPOSE, "exec", "-T", "fn1", "python3", "-c",
                        "import socket; socket.create_connection(('1.1.1.1', 53), timeout=4)"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0


def test_host_credentials_are_not_in_any_container(control):
    host = {k: os.environ[k] for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN") if os.environ.get(k)}
    ids = subprocess.run([*COMPOSE, "ps", "-q"], capture_output=True, text=True).stdout.split()
    text = subprocess.run(["docker", "inspect", *ids], capture_output=True, text=True).stdout if ids else ""
    for name, value in host.items():
        assert value not in text, f"{name} of the host is present in a container"


def test_writes_are_on_only_because_every_address_is_a_mock(only_mock_addresses):
    assert only_mock_addresses["SYTELINE_LIVE_WRITES"] == "true"
    assert json.load(open(os.path.join(RUN_DIR, "rendered.json")))["env"]["SYTELINE_WSDL"].startswith("http://erp:")
