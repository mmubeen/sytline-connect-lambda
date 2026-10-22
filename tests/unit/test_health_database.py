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
