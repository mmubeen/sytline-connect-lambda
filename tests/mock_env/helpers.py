"""Shared helpers of the mock-environment tests."""

import os
import shlex
import subprocess

EDGE = os.environ.get("MOCK_EDGE_URL", "http://127.0.0.1:4566")
RUN_DIR = os.environ.get("MOCK_RUN_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "mock", ".run"))


def wait_for(predicate, timeout=60, interval=1.0):
    """Poll until `predicate()` returns something truthy; return it, or None after `timeout` seconds."""
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate() or None


def erp_saves(control, since=0):
    """The SaveDataSet calls the ERP mock answered successfully."""
    return [c for c in control.calls(service="erp", since=since) if c["operation"] == "SaveDataSet" and c["status"] == 200]


def compose_command():
    """The compose command as a list: MOCK_COMPOSE (like `make DOCKER_COMPOSE=...`), else `docker compose`, else `docker-compose`."""
    if os.environ.get("MOCK_COMPOSE"):
        return shlex.split(os.environ["MOCK_COMPOSE"])
    import shutil

    try:
        if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
            return ["docker", "compose"]
    except OSError:       # no docker at all: collecting the tests must still work (they skip)
        pass

    return ["docker-compose"] if shutil.which("docker-compose") else ["docker", "compose"]


COMPOSE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "mock", "compose.yaml")
