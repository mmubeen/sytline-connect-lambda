"""Integration tests against the production-like mock environment (specs/008). Skipped unless MOCK_ENV=1.

Start the environment first: `make -C mock up`. See docs/MOCK_ENVIRONMENT.md.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "mock"))

from .helpers import EDGE  # noqa: E402
NOT_RUNNING = "mock environment not running: start it with `make -C mock up`"


from .helpers import RUN_DIR  # noqa: E402


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="set MOCK_ENV=1 (and start `make -C mock up`) to run the mock-environment tests")
    for item in items:
        if "tests/mock_env" not in str(item.fspath).replace(os.sep, "/"):
            continue
        item.add_marker(pytest.mark.mockenv)
        if os.environ.get("MOCK_ENV") != "1":
            item.add_marker(skip)


class Control:
    """Client of the mock control API (contracts/control-api.md)."""

    def __init__(self, base):
        self.base = base.rstrip("/") + "/__mock__"

    def _call(self, method, path, **kw):
        import requests

        r = requests.request(method, self.base + path, timeout=kw.pop("timeout", 330), **kw)
        r.raise_for_status()
        return r.json()

    def health(self):
        return self._call("GET", "/health", timeout=5)

    def reset(self):
        return self._call("POST", "/reset")

    def scenario(self, name):
        return self._call("POST", f"/scenario/{name}")

    def fault(self, fault_id, remaining=None, **params):
        return self._call("POST", "/faults", json={"id": fault_id, "params": params, "remaining": remaining})

    def clear_faults(self):
        return self._call("DELETE", "/faults")

    def calls(self, service=None, since=0):
        q = {"since": since, **({"service": service} if service else {})}
        return self._call("GET", "/calls", params=q)

    def invoke(self, event, mode="sync", timeout=None):
        return self._call("POST", "/invoke", json={"event": event, "mode": mode, "timeout": timeout})

    def sweep(self):
        return self._call("POST", "/tick/sweep")


@pytest.fixture(scope="session", autouse=True)
def only_mock_addresses():
    """FR-011: refuse to run when the function's ERP or AWS address is not a mock address (writes are ON inside the mock)."""
    from edge import prepare

    path = os.path.join(RUN_DIR, "function.env")
    if not os.path.exists(path):
        pytest.exit(NOT_RUNNING, returncode=3)
    env = dict(line.split("=", 1) for line in open(path).read().splitlines() if "=" in line)
    bad = prepare.foreign_addresses(env)
    if bad:
        pytest.exit("refusing to run: these settings do not point at a mock service: " + ", ".join(bad), returncode=4)
    return env


@pytest.fixture(scope="session")
def control(only_mock_addresses):
    c = Control(EDGE)
    try:
        health = c.health()
    except Exception:
        pytest.exit(NOT_RUNNING, returncode=3)
    if not health.get("ready"):
        pytest.exit(f"{NOT_RUNNING} (not ready: {health.get('services')})", returncode=3)
    return c


@pytest.fixture(autouse=True)
def fresh(control):
    control.reset()
    yield
    control.clear_faults()

