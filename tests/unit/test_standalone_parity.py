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
            "'syteline.resolver')]; print(','.join(bad))" % os.path.join(ROOT, "src"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"direct handler imports order-service modules: {out.stdout}"


# ---- the optional database connectivity check (feature 005, revision 2026-10-22) ------------------------------------
def _db_env(monkeypatch, kind):
    from fakes import DB_CFG
    from syteline import idos

    for k, v in DB_CFG.items():
        monkeypatch.setenv(k, v)
    if kind == "vpc":  # the plant version switches the stages on through its route settings
        for k in (idos.NEXT_CO_SETTING, idos.NEXT_JOB_SETTING, idos.CUSTOMER_SETTING, idos.DEFAULTS_SETTING):
            monkeypatch.setenv(k, "database")
    else:              # the stand-alone version has its own switch and knows whether it is attached
        monkeypatch.setenv("SYTELINE_DB_CHECK", "true")
        monkeypatch.setenv("SYTELINE_ATTACHED", "true")


def _fake_db_network(monkeypatch):
    import socket as _s
    from fakes import DB_CFG

    class C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real_gai, real_cc = _s.getaddrinfo, _s.create_connection
    monkeypatch.setattr(_s, "getaddrinfo", lambda h, p, *a, **k: [(2, 1, 6, "", ("10.1.2.3", p))] if h == DB_CFG["SQL_SERVER"]
                        else real_gai(h, p, *a, **k))
    monkeypatch.setattr(_s, "create_connection", lambda addr, *a, **k: C() if addr[0] == DB_CFG["SQL_SERVER"]
                        else real_cc(addr, *a, **k))


def test_by_default_the_standalone_health_has_no_database_stage_and_opens_no_connection(modes, monkeypatch):
    from fakes import FakeSqlServer

    modes("standalone")
    f = FakeSqlServer().install(monkeypatch)
    out = lf.lambda_handler({"action": "health"}, None)
    assert [s["stage"] for s in out["stages"]] == ["dns", "tcp"] and "database_check" not in out
    assert f.connect_kwargs == []


@pytest.mark.parametrize("encrypt", ["true", "false"])
def test_with_the_check_on_and_attached_the_standalone_stages_and_warning_equal_the_plants(modes, monkeypatch, encrypt):
    from fakes import DB_CFG, FakeSqlServer
    from syteline import health

    monkeypatch.setenv("SYTELINE_SQL_ENCRYPT", encrypt)
    if encrypt == "false":
        monkeypatch.setenv("SQL_CA_PEM", "")
    seen = {}
    for kind in ("standalone", "vpc"):
        modes(kind)
        _db_env(monkeypatch, kind)
        if encrypt == "false":
            monkeypatch.setenv("SQL_CA_PEM", "")
        _fake_db_network(monkeypatch)
        FakeSqlServer().install(monkeypatch)
        out = health.run_health({"action": "health"})
        stages = [(s["stage"], s["ok"]) for s in out["stages"] if s["stage"].startswith("db_")]
        seen[kind] = (stages, [s.get("detail", "")[:11] for s in out["stages"] if s["stage"] == "db_login"],
                      [w for w in out.get("warnings", []) if "UNENCRYPTED" in w])
    assert seen["standalone"] == seen["vpc"]
    assert [n for n, _ in seen["vpc"][0]] == ["db_dns", "db_tcp", "db_login"]
    assert bool(seen["vpc"][2]) == (encrypt == "false")


def test_the_standalone_notes_actions_never_open_a_database_connection(modes, monkeypatch):
    from fakes import FakeSqlServer

    modes("standalone")
    _db_env(monkeypatch, "standalone")
    f = FakeSqlServer().install(monkeypatch)
    soap = FakeSoap()
    monkeypatch.setattr(lf, "_client_factory", lambda: soap)
    monkeypatch.setenv("SYTELINE_LIVE_WRITES", "true")
    soap.add_record("SLCos", CoNum="900", RowPointer="rp-1")
    for event in ({"action": "get_notes", "record_type": "co", "record_key": "900"},
                  {"action": "add_note", "record_type": "co", "record_key": "900", "text": "hi"}):
        lf.lambda_handler(event, None)
    assert f.connect_kwargs == [] and f.statements == []
