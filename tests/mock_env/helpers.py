"""Shared helpers of the mock-environment tests."""

import os

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
