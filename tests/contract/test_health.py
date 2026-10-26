import lambda_function as lf


def _clear(monkeypatch):
    for k in ("SYTELINE_WSDL", "SYTELINE_USER", "SYTELINE_PASS", "SYTELINE_SECRET_ARN", "SYTELINE_DRY_RUN"):
        monkeypatch.delenv(k, raising=False)


def test_health_reports_missing_config(monkeypatch):
    _clear(monkeypatch)
    out = lf.lambda_handler({"action": "health"}, None)
    assert out["ok"] is False and out["stages"] == [] and "missing config" in out["error"]


def test_a_bare_payload_is_no_longer_a_health_check(monkeypatch):
    # the default action is document_event (specs/006): a bare payload is refused and says how to ask for the health check
    _clear(monkeypatch)
    out = lf.lambda_handler({}, None)
    assert out["ok"] is False and out["errors"][0]["code"] == "EVENT_INVALID" and "stages" not in out
    assert '"action": "health"' in out["errors"][0]["message"]


def test_dry_run_needs_only_wsdl_and_stage_order(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SYTELINE_WSDL", "http://127.0.0.1:9/x?wsdl")
    out = lf.lambda_handler({"action": "health", "dry_run": True}, None)
    assert set(out) == {"ok", "dry_run", "host", "config", "stages", "routes", "allowed_config_versions"}
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
    out = lf.lambda_handler({"action": "health"}, None)
    assert [s["stage"] for s in out["stages"]] == ["dns", "tcp"]  # stops at first failure


def test_unknown_action():
    out = lf.lambda_handler({"action": "nope"}, None)
    assert out["ok"] is False and out["errors"][0]["code"] == "UNKNOWN_ACTION"


def test_dry_run_never_creates_a_session_and_check_sql_has_no_effect(monkeypatch):
    import zeep

    def boom(*a, **k):
        raise AssertionError("dry run must not create a SOAP client / log in")

    monkeypatch.setattr(zeep, "Client", boom)
    monkeypatch.setenv("SYTELINE_WSDL", "http://127.0.0.1:9/x?wsdl")
    out = lf.lambda_handler({"action": "health", "dry_run": True, "check_sql": True}, None)
    assert [s["stage"] for s in out["stages"]][:3] == ["load", "dns", "tcp"]
    monkeypatch.setenv("SYTELINE_USER", "u")
    monkeypatch.setenv("SYTELINE_PASS", "p")
    out = lf.lambda_handler({"action": "health", "check_sql": True}, None)
    assert [s["stage"] for s in out["stages"]] == ["dns", "tcp"]  # unchanged; no sql stage exists
