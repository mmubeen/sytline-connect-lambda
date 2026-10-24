"""US5: the opt-in real-IDP mode (needs `make -C mock up MOCK_IDP=real ...` and the developer's own access to a real IDP).

Skipped unless MOCK_ENV=1 and MOCK_IDP=real. The outcome for a real document is reported, not asserted: these tests check only the
invariants (a known outcome, only allow-listed read calls left the machine, nothing was written to a real service).
"""

import json
import os
import time

import pytest

from edge import realidp
from .helpers import RUN_DIR

pytestmark = pytest.mark.skipif(os.environ.get("MOCK_IDP") != "real", reason="set MOCK_IDP=real (real-IDP mode is opt-in)")

KNOWN_OUTCOMES = {"refused", "read_only", "reserved", "save_accepted", "ignored", "already_processed"}
IDP_DIR = os.path.join(os.path.dirname(RUN_DIR), ".idp")


def aws_calls(control, target):
    return [c for c in control.calls(service="aws") if c["target"] == target]


def test_the_real_document_gets_a_known_outcome_and_only_idp_reads_left_the_machine(control):
    event = control.scenario("real-idp")["event"]
    result = control.invoke(event)
    assert result["status"] == 200, result
    out = result["payload"]
    print("outcome:", out.get("outcome"), [e.get("code") for e in out.get("errors", [])])    # reported, not asserted
    assert out["outcome"] in KNOWN_OUTCOMES, out
    real = aws_calls(control, "real")
    assert real, "no call reached the real IDP"
    assert {c["action"] for c in real} <= realidp.READ_ONLY
    assert all(c["allowed"] for c in real), [c for c in real if not c["allowed"]]
    # the ERP is the mock: nothing in the call records points anywhere else
    assert {c["service"] for c in control.calls() if c["service"] not in ("aws", "erp")} == set()


def test_mock_scenarios_are_not_available_in_this_mode(control):
    import requests

    from .helpers import EDGE

    r = requests.post(EDGE + "/__mock__/scenario/passes-review", timeout=30)
    assert r.status_code == 409 and "real-IDP mode" in r.json()["error"]


def test_expired_credentials_are_reported_with_the_reason_and_nothing_falls_back(control):
    creds = os.path.join(IDP_DIR, "creds")
    keep = open(creds).read()
    try:
        lines = [ln for ln in keep.splitlines() if not ln.startswith("AWS_CREDENTIAL_EXPIRATION")]
        open(creds, "w").write("\n".join(lines + ["AWS_CREDENTIAL_EXPIRATION=2020-01-01T00:00:00Z"]) + "\n")
        os.chmod(creds, 0o600)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and "expired" not in json.dumps(control.health()):
            time.sleep(1)
        assert "credentials expired" in control.health()["services"]["idp"]       # SC-007: within 30 seconds
        from edge import events

        control.reset()
        out = control.invoke(events.build_event(json.load(open(os.path.join(IDP_DIR, "mode.json")))["document"]))["payload"]
        assert out["outcome"] == "refused" and "IDP_UNAVAILABLE" in [e["code"] for e in out["errors"]]
        denied = [c for c in aws_calls(control, "real") if not c["allowed"]]
        assert denied and not [c for c in aws_calls(control, "real") if c["allowed"]]     # nothing went out, no mock fallback
    finally:
        open(creds, "w").write(keep)
        os.chmod(creds, 0o600)
