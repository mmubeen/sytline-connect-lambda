import lambda_function as lf


def _clear(monkeypatch):
    for k in ("SYTELINE_WSDL", "SYTELINE_USER", "SYTELINE_PASS", "SYTELINE_SECRET_ARN", "SYTELINE_DRY_RUN"):
        monkeypatch.delenv(k, raising=False)


def test_default_action_is_health_and_reports_missing_config(monkeypatch):
    _clear(monkeypatch)
    out = lf.lambda_handler({}, None)
    assert out["ok"] is False and out["stages"] == [] and "missing config" in out["error"]


def test_dry_run_needs_only_wsdl_and_stage_order(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SYTELINE_WSDL", "http://127.0.0.1:9/x?wsdl")
    out = lf.lambda_handler({"action": "health", "dry_run": True}, None)
    assert set(out) == {"ok", "dry_run", "host", "config", "stages"}
    assert out["dry_run"] is True
    names = [s["stage"] for s in out["stages"]]
    assert names[0] == "load" and names[:3] == ["load", "dns", "tcp"]
    assert out["ok"] is False
    assert names[-1] == "tcp" and out["stages"][-1]["ok"] is False  # nothing listens on :9; stops there


def test_normal_run_stage_names(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SYTELINE_WSDL", "http://127.0.0.1:9/x?wsdl")
    monkeypatch.setenv("SYTELINE_USER", "u")
    monkeypatch.setenv("SYTELINE_PASS", "p")
    out = lf.lambda_handler({}, None)
    assert [s["stage"] for s in out["stages"]] == ["dns", "tcp"]  # stops at first failure


def test_unknown_action():
    out = lf.lambda_handler({"action": "nope"}, None)
    assert out["ok"] is False and out["errors"][0]["code"] == "UNKNOWN_ACTION"


def test_dry_run_never_opens_sql_and_default_stage_list_is_unchanged(monkeypatch):
    import syteline.sql as sqlmod

    def boom(*a, **k):
        raise AssertionError("SQL must not be touched by health")

    monkeypatch.setattr(sqlmod.SqlClient, "_open", boom)
    monkeypatch.setenv("SYTELINE_WSDL", "http://127.0.0.1:9/x?wsdl")
    monkeypatch.setenv("SYTELINE_SQL_HOST", "127.0.0.1")
    monkeypatch.setenv("SYTELINE_SQL_PORT", "9")
    out = lf.lambda_handler({"action": "health", "dry_run": True, "check_sql": True}, None)
    assert "sql" not in [s["stage"] for s in out["stages"]]
    monkeypatch.setenv("SYTELINE_USER", "u")
    monkeypatch.setenv("SYTELINE_PASS", "p")
    out = lf.lambda_handler({}, None)
    assert [s["stage"] for s in out["stages"]] == ["dns", "tcp"]  # SQL configured, but not requested


def test_sql_stage_is_opt_in_non_dry_tcp_only(monkeypatch):
    import socket

    from syteline import health

    monkeypatch.setenv("SYTELINE_WSDL", "http://127.0.0.1:9/x?wsdl")
    monkeypatch.setenv("SYTELINE_USER", "u")
    monkeypatch.setenv("SYTELINE_PASS", "p")
    monkeypatch.setenv("SYTELINE_SQL_HOST", "sqlhost")
    monkeypatch.setenv("SYTELINE_SQL_PORT", "14330")
    seen = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.0.0.1", 80))])
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: seen.append(addr) or Conn())

    class Svc:
        def CreateSessionToken(self, *a):
            return "tok"

    class Client:
        service = Svc()

    import zeep

    monkeypatch.setattr(zeep, "Client", lambda *a, **k: Client())
    out = health.run_health({"check_sql": True})
    assert [s["stage"] for s in out["stages"]] == ["dns", "tcp", "wsdl", "token", "sql"] and out["ok"]
    assert ("sqlhost", 14330) in seen
    out = health.run_health({})
    assert [s["stage"] for s in out["stages"]] == ["dns", "tcp", "wsdl", "token"]
