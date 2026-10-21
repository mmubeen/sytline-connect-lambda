"""The stand-alone deployment gives the same results as the VPC deployment for the shared operations (US2, SC-003)."""

import os
import socket
import subprocess
import sys

import pytest

import lambda_function as lf
from fakes import FakeSoap

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


@pytest.fixture
def modes(monkeypatch):
    """Return a function that applies the 'standalone' or 'vpc' environment."""
    def apply(kind):
        for k in ("SYTELINE_REQUIRE_HTTPS", "SYTELINE_ALLOW_HTTP", "JOBS_TABLE", "WORKER_FUNCTION_NAME"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("SYTELINE_WSDL", "https://127.0.0.1:9/x?wsdl")
        monkeypatch.setenv("SYTELINE_USER", "u")
        monkeypatch.setenv("SYTELINE_PASS", "p")
        if kind == "standalone":
            monkeypatch.setenv("SYTELINE_REQUIRE_HTTPS", "true")
            monkeypatch.setenv("SYTELINE_VERIFY_TLS", "true")
        else:
            monkeypatch.setenv("JOBS_TABLE", "t")
            monkeypatch.setenv("WORKER_FUNCTION_NAME", "w")
    return apply


def strip_timing(out):
    out = dict(out)
    out["stages"] = [{k: v for k, v in s.items() if k not in ("ms", "error", "detail")} for s in out.get("stages", [])]
    return out


def test_dry_run_health_has_the_same_stages_and_shape(modes):
    results = {}
    for kind in ("standalone", "vpc"):
        modes(kind)
        results[kind] = strip_timing(lf.lambda_handler({"action": "health", "dry_run": True}, None))
    assert results["standalone"] == results["vpc"]
    assert [s["stage"] for s in results["vpc"]["stages"]][:3] == ["load", "dns", "tcp"]
    assert "warnings" not in results["standalone"] and "errors" not in results["standalone"]


def test_full_health_has_the_same_stage_names(modes):
    names = {}
    for kind in ("standalone", "vpc"):
        modes(kind)
        names[kind] = [s["stage"] for s in lf.lambda_handler({"action": "health"}, None)["stages"]]
    assert names["standalone"] == names["vpc"] == ["dns", "tcp"]  # stops at the first failure


def _notes(modes, kind, monkeypatch, live):
    modes(kind)
    if live:
        monkeypatch.setenv("SYTELINE_LIVE_WRITES", "true")
    else:
        monkeypatch.delenv("SYTELINE_LIVE_WRITES", raising=False)
    s = FakeSoap()
    s.add_record("SLCos", CoNum="100")
    monkeypatch.setattr(lf, "_client_factory", lambda: s)
    call = lambda **kw: lf.lambda_handler(kw, None)
    return [
        call(action="add_note", record_type="co", record_key="100", text="hello"),
        call(action="get_notes", record_type="co", record_key="100"),
        call(action="get_notes", record_type="job", record_key="nope"),
        call(action="add_note", record_type="so", record_key=""),
    ]


@pytest.mark.parametrize("live", [False, True])
def test_notes_operations_give_identical_results_and_error_codes(modes, monkeypatch, live):
    a = _notes(modes, "standalone", monkeypatch, live)
    b = _notes(modes, "vpc", monkeypatch, live)
    assert a == b
    if not live:
        assert a[0]["errors"][0]["code"] == "LIVE_WRITES_DISABLED"  # off by default in both
    assert a[2]["errors"][0]["code"] == "NOT_FOUND" and {e["field"] for e in a[3]["errors"]} == {
        "record_type", "record_key", "text"}


def test_order_upload_is_absent(modes):
    modes("standalone")
    for action in ("create_order", "get_status", "upload", "sweep"):
        out = lf.lambda_handler({"action": action}, None)
        assert out["ok"] is False and out["errors"][0]["code"] == "UNKNOWN_ACTION"


def test_the_direct_handler_does_not_import_the_order_service():
    """The direct handler must not pull in the job store, order handlers, worker or sweeper (US2)."""
    code = ("import sys; sys.path.insert(0, %r); import lambda_function; "
            "bad=[m for m in sys.modules if m.startswith('handlers') or m in ('syteline.jobs','syteline.pipeline',"
            "'syteline.resolver')]; print(','.join(bad))" % os.path.join(ROOT, "scr"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"direct handler imports order-service modules: {out.stdout}"
