import logging

import pytest

import lambda_function as lf
from fakes import DB_CFG, FakeSqlServer
from syteline import health, idos, routes

OPS = {"next_co": idos.NEXT_CO_SETTING, "next_job": idos.NEXT_JOB_SETTING, "customer": idos.CUSTOMER_SETTING,
       "defaults": idos.DEFAULTS_SETTING}


@pytest.fixture
def env(monkeypatch):
    for k in ("SYTELINE_WSDL", "SYTELINE_USER", "SYTELINE_PASS", "SYTELINE_SECRET_ARN", "SYTELINE_DRY_RUN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SYTELINE_WSDL", "http://127.0.0.1:9/x?wsdl")
    monkeypatch.setenv("SYTELINE_USER", "u")
    monkeypatch.setenv("SYTELINE_PASS", "p")
    return monkeypatch


def database_everywhere(monkeypatch):
    for v in OPS.values():
        monkeypatch.setenv(v, "database")


def with_db_settings(monkeypatch, **override):
    for k, v in dict(DB_CFG, **override).items():
        monkeypatch.setenv(k, v)


def fake_network(monkeypatch, resolves=True, connects=True):
    import socket

    real_gai, real_cc = socket.getaddrinfo, socket.create_connection

    def gai(host, port, *a, **k):
        if host == DB_CFG["SQL_SERVER"]:
            if not resolves:
                raise socket.gaierror(8, "nodename nor servname provided")
            return [(2, 1, 6, "", ("10.1.2.3", port))]
        return real_gai(host, port, *a, **k)

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def cc(addr, *a, **k):
        if addr[0] == DB_CFG["SQL_SERVER"]:
            if not connects:
                raise TimeoutError("timed out")
            return Conn()
        return real_cc(addr, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", gai)
    monkeypatch.setattr(socket, "create_connection", cc)


def db_stages(out):
    return [s for s in out["stages"] if s["stage"].startswith("db_")]


def test_routes_are_in_every_response(env):
    out = lf.lambda_handler({"action": "health", "dry_run": True}, None)
    assert out["routes"] == routes.describe() == {"next_co": "webservice:SLCos", "next_job": "webservice:SLJobs",
                                                   "customer": "webservice:SLCustAddrs", "defaults": "off"}


def test_the_dry_run_reports_routes_only_and_opens_no_connection(env):
    database_everywhere(env)
    f = FakeSqlServer().install(env)
    out = lf.lambda_handler({"action": "health", "dry_run": True}, None)
    assert out["routes"] == {k: "database" for k in OPS} and db_stages(out) == [] and f.connect_kwargs == []


def test_a_full_run_with_a_database_route_adds_the_three_stages_after_token(env):
    database_everywhere(env)
    with_db_settings(env)
    fake_network(env)
    f = FakeSqlServer().install(env)
    out = health.run_health({"action": "health"})
    names = [s["stage"] for s in out["stages"]]
    assert names[-3:] == ["db_dns", "db_tcp", "db_login"] and names[:2] == ["dns", "tcp"]
    assert [s["ok"] for s in db_stages(out)] == [True, True, True]
    assert f.opened == f.closed == 1 and [s for s, _ in f.statements] == ["SELECT 1"]  # a harmless read, no write


def test_no_database_stages_when_no_operation_uses_the_database(env):
    for v in OPS.values():
        env.setenv(v, "SomeEndpoint")
    f = FakeSqlServer().install(env)
    out = health.run_health({"action": "health"})
    assert db_stages(out) == [] and f.connect_kwargs == []
    assert [s["stage"] for s in out["stages"]][:2] == ["dns", "tcp"]  # the existing stages are unchanged


def test_each_database_stage_fails_on_its_own_and_later_ones_are_skipped(env):
    database_everywhere(env)
    with_db_settings(env)
    fake_network(env, resolves=False)
    out = health.run_health({"action": "health"})
    assert [(s["stage"], s["ok"]) for s in db_stages(out)] == [("db_dns", False)] and out["ok"] is False
    fake_network(env, connects=False)
    out = health.run_health({"action": "health"})
    assert [(s["stage"], s["ok"]) for s in db_stages(out)] == [("db_dns", True), ("db_tcp", False)]


@pytest.mark.parametrize("fake_kwargs,expected", [
    ({"connect_error": "login_fail"}, "login refused"),
    ({"connect_error": "connect_fail"}, "database connection failed")])
def test_login_failures_are_plain_and_never_show_credentials(env, fake_kwargs, expected, caplog):
    caplog.set_level(logging.DEBUG)
    database_everywhere(env)
    with_db_settings(env)
    fake_network(env)
    FakeSqlServer(**fake_kwargs).install(env)
    out = health.run_health({"action": "health"})
    login = [s for s in out["stages"] if s["stage"] == "db_login"][0]
    assert login["ok"] is False and expected in login["error"] and out["ok"] is False
    text = repr(out) + caplog.text
    assert DB_CFG["SQL_PASS"] not in text and DB_CFG["SQL_USER"] not in text


@pytest.mark.parametrize("raised,expected", [
    ("certificate verify failed: unable to get local issuer certificate", "certificate not trusted"),
    ("Certificate does not match host name 'sql.plant.example'", "host name does not match the certificate")])
def test_certificate_problems_are_named_in_plain_words(env, raised, expected):
    database_everywhere(env)
    with_db_settings(env)
    fake_network(env)

    def boom(**kw):
        raise RuntimeError(raised)

    from syteline import sqlserver

    env.setattr(sqlserver, "_connect", boom)
    out = health.run_health({"action": "health"})
    login = [s for s in out["stages"] if s["stage"] == "db_login"][0]
    assert expected in login["error"]


def test_missing_database_settings_are_named_by_key_only(env):
    database_everywhere(env)
    with_db_settings(env, SQL_CA_PEM="")
    fake_network(env)
    f = FakeSqlServer().install(env)
    out = health.run_health({"action": "health"})
    login = [s for s in out["stages"] if s["stage"] == "db_login"][0]
    assert "database not configured" in login["error"] and "SQL_CA_PEM" in login["error"]
    assert f.connect_kwargs == [] and DB_CFG["SQL_SERVER"] not in login["error"]


def test_a_failing_web_service_does_not_hide_the_database_result(env):
    database_everywhere(env)
    with_db_settings(env)
    fake_network(env)
    FakeSqlServer().install(env)
    out = health.run_health({"action": "health"})  # nothing listens on 127.0.0.1:9: the tcp stage fails
    assert [s["ok"] for s in out["stages"] if s["stage"] == "tcp"] == [False]
    assert [s["stage"] for s in db_stages(out)] == ["db_dns", "db_tcp", "db_login"] and out["ok"] is False


# ---- the explicit unencrypted option (revision 2026-10-22) ------------------------------------------------------
UNENC_WARNING = "database connection is UNENCRYPTED: the login and password cross the network unprotected"


def test_the_default_mode_says_encrypted_and_has_no_warning(env):
    database_everywhere(env)
    with_db_settings(env)
    fake_network(env)
    FakeSqlServer().install(env)
    out = health.run_health({"action": "health"})
    login = [s for s in out["stages"] if s["stage"] == "db_login"][0]
    assert login["ok"] is True and login["detail"].startswith("encrypted connection")
    assert UNENC_WARNING not in out.get("warnings", [])


def test_the_explicit_unencrypted_option_needs_no_ca_and_is_reported_in_the_stage_and_as_a_warning(env):
    database_everywhere(env)
    with_db_settings(env, SQL_CA_PEM="")
    env.setenv("SYTELINE_SQL_ENCRYPT", "false")
    fake_network(env)
    f = FakeSqlServer().install(env)
    out = health.run_health({"action": "health"})
    login = [s for s in out["stages"] if s["stage"] == "db_login"][0]
    assert login["ok"] is True and login["detail"].startswith("UNENCRYPTED connection")
    assert UNENC_WARNING in out["warnings"]
    assert "cafile" not in f.connect_kwargs[0]
    assert DB_CFG["SQL_PASS"] not in repr(out)


def test_missing_keys_omit_the_ca_when_unencrypted_and_name_it_when_encrypted(env):
    database_everywhere(env)
    with_db_settings(env, SQL_CA_PEM="", SQL_PASS="")
    fake_network(env)
    FakeSqlServer().install(env)
    env.setenv("SYTELINE_SQL_ENCRYPT", "false")
    msg = [s for s in health.run_health({"action": "health"})["stages"] if s["stage"] == "db_login"][0]["error"]
    assert "SQL_PASS" in msg and "SQL_CA_PEM" not in msg
    env.delenv("SYTELINE_SQL_ENCRYPT")
    msg = [s for s in health.run_health({"action": "health"})["stages"] if s["stage"] == "db_login"][0]["error"]
    assert "SQL_PASS" in msg and "SQL_CA_PEM" in msg


def test_no_warning_and_no_database_stage_when_no_operation_uses_the_database(env):
    for v in OPS.values():
        env.setenv(v, "SomeEndpoint")
    env.setenv("SYTELINE_SQL_ENCRYPT", "false")
    out = health.run_health({"action": "health"})
    assert db_stages(out) == [] and UNENC_WARNING not in out.get("warnings", [])


# ---- the stand-alone database check (revision 2026-10-22) -------------------------------------------------------
# ---- the database probe of the diagnostics runs exactly when the stages would (spec FR-019) -----------------------------
def stub_probes(monkeypatch):
    """Record which hosts the diagnostics probe, without any real lookup or connection."""
    from syteline import diagnostics as dg

    seen = []

    def probe(host, port, timeout):
        seen.append(host)
        return {"host": host, "port": port, "ok": True, "attempts": [], "addresses": ["10.1.2.3"],
                "connections": [{"address": "10.1.2.3", "ok": True}], "connected": True}

    monkeypatch.setattr(dg, "probe_host", probe)
    monkeypatch.setattr(dg, "raw_probe", lambda name, ns: [])
    monkeypatch.setattr(dg, "read_resolv", lambda *a, **k: {"nameservers": [], "search": []})
    monkeypatch.setattr(dg, "local_info", lambda: {})
    return seen


def without_diagnostics(out):
    """The result without the diagnostics section and without the timings (they differ from run to run)."""
    body = {k: v for k, v in out.items() if k != "diagnostics"}
    body["stages"] = [{k: v for k, v in st.items() if k != "ms"} for st in body.get("stages", [])]
    return body


def probed(env, **event):
    seen = stub_probes(env)
    fake_network(env)
    FakeSqlServer().install(env)
    out = health.run_health({"action": "health", **event})
    return out, seen


def test_plant_with_a_database_route_probes_the_database_and_leaves_the_stages_alone(env):
    database_everywhere(env)
    with_db_settings(env)
    plain, _ = probed(env)
    out, seen = probed(env, diagnose=True)
    assert DB_CFG["SQL_SERVER"] in seen and out["diagnostics"]["database"]["host"] == DB_CFG["SQL_SERVER"]
    assert any("database host and port are reachable" in f for f in out["diagnostics"]["findings"])
    assert without_diagnostics(out) == without_diagnostics(plain)  # stages, ok and warnings are the same without diagnose


def test_plant_with_every_route_on_the_web_service_does_not_probe_the_database(env):
    with_db_settings(env)
    out, seen = probed(env, diagnose=True)
    assert DB_CFG["SQL_SERVER"] not in seen
    assert "database" not in out["diagnostics"] and "database_skipped" not in out["diagnostics"]


def test_a_dry_run_says_why_the_database_was_not_probed(env):
    database_everywhere(env)
    with_db_settings(env)
    out, seen = probed(env, diagnose=True, dry_run=True)
    assert DB_CFG["SQL_SERVER"] not in seen and "database" not in out["diagnostics"]
    assert out["diagnostics"]["database_skipped"] == health.DRY_RUN_DB_SKIP


def test_an_unreadable_secret_says_why_the_database_was_not_probed(env):
    from syteline import config as cfgmod, errors

    database_everywhere(env)
    seen = stub_probes(env)

    def boom(skip_secret=False):
        if skip_secret:
            return {"SYTELINE_WSDL": "http://127.0.0.1:9/x?wsdl"}
        raise errors.SecretStoreUnavailable("secret store unreachable")

    env.setattr(cfgmod, "load_config", boom)
    out = health.run_health({"action": "health", "diagnose": True})
    assert out["diagnostics"]["database_skipped"] == health.SECRET_DB_SKIP and DB_CFG["SQL_SERVER"] not in seen
