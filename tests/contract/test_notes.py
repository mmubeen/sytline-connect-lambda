import lambda_function as lf
from fakes import FakeSoap


def setup(monkeypatch):
    s = FakeSoap()
    s.add_record("SLCos", CoNum="100")
    s.add_record("CCTJobs", Job="5000")
    monkeypatch.setattr(lf, "_client_factory", lambda: s)
    return s


def call(**kw):
    return lf.lambda_handler(kw, None)


def test_add_and_get_roundtrip_internal_external_unicode_long(monkeypatch, live_writes):
    setup(monkeypatch)
    long_text = "héllo wörld — ünïcode " * 50
    assert call(action="add_note", record_type="job", record_key="5000", text=long_text, internal=True)["ok"]
    assert call(action="add_note", record_type="job", record_key="5000", text="external", internal=False)["ok"]
    out = call(action="get_notes", record_type="job", record_key="5000")
    assert out["ok"] and len(out["notes"]) == 2
    assert out["notes"][0]["text"] == long_text and out["notes"][0]["internal"] is True
    assert out["notes"][1]["internal"] is False
    assert set(out["notes"][0]) == {"token", "text", "internal", "created_at", "created_by"}


def test_since_filter(monkeypatch, live_writes):
    setup(monkeypatch)
    call(action="add_note", record_type="co", record_key="100", text="t")
    assert len(call(action="get_notes", record_type="co", record_key="100", since="2026-10-01")["notes"]) == 1
    assert call(action="get_notes", record_type="co", record_key="100", since="2026-11-01")["notes"] == []


def test_not_found_writes_nothing(monkeypatch, live_writes):
    s = setup(monkeypatch)
    out = call(action="add_note", record_type="co", record_key="999", text="x")
    assert out["errors"][0]["code"] == "NOT_FOUND" and s.writes() == []
    assert call(action="get_notes", record_type="job", record_key="nope")["errors"][0]["code"] == "NOT_FOUND"


def test_validation(monkeypatch):
    setup(monkeypatch)
    out = call(action="add_note", record_type="so", record_key="")
    assert {e["field"] for e in out["errors"]} == {"record_type", "record_key", "text"}


def test_live_writes_disabled_error_for_add_note_only(monkeypatch):
    s = setup(monkeypatch)
    assert call(action="add_note", record_type="co", record_key="100", text="x")["errors"][0]["code"] == "LIVE_WRITES_DISABLED"
    assert call(action="get_notes", record_type="co", record_key="100")["ok"] is True
    assert s.writes() == []
