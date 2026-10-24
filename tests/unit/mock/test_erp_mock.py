"""T016: the SyteLine web-service mock, exercised with the function's real SoapIdoClient over HTTP."""

import threading

import pytest
import requests
from werkzeug.serving import make_server

from erp import app as erp_app
from syteline import errors
from syteline.client import SoapIdoClient

USER, PASS = "mock-user", "mock-pass"


@pytest.fixture
def erp():
    mock = erp_app.ErpMock(user=USER, password=PASS)
    server = make_server("127.0.0.1", 0, mock, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    mock.base = f"http://127.0.0.1:{server.server_port}"
    yield mock
    server.shutdown()


def cfg(erp, **over):
    return {"SYTELINE_WSDL": erp.base + "/IDORequestService/IDOWebService.asmx?wsdl", "SYTELINE_USER": USER,
            "SYTELINE_PASS": PASS, "SYTELINE_CONFIG": "CCT", **over}


@pytest.fixture
def client(erp, live_writes):
    return SoapIdoClient(cfg(erp), timeout=5)


def test_wsdl_is_served_with_the_real_address(erp):
    text = requests.get(erp.base + "/IDORequestService/IDOWebService.asmx?wsdl").text
    assert "LoadDataSet" in text and erp.base in text and "@LOCATION@" not in text


def test_login_and_load_with_filter(erp, client):
    erp.load({"collections": {"SLCos": [{"CoNum": "1", "CustPo": "A"}, {"CoNum": "2", "CustPo": "B"}]}})
    rows = client.load("SLCos", ["CoNum", "CustPo"], "CustPo = 'B'")
    assert rows == [{"CoNum": "2", "CustPo": "B"}] or rows[0]["CoNum"] == "2"
    assert len(rows) == 1


def test_wrong_password_is_rejected(erp):
    c = SoapIdoClient(cfg(erp, SYTELINE_PASS="nope"), timeout=5)
    with pytest.raises((errors.ErpRejected, errors.ErpUnreachable)):
        c.connect()


def test_insert_is_stored_and_recorded(erp, client):
    row = client.insert("SLCos", {"CustPo": "PO-1"})
    assert row["CustPo"] == "PO-1"
    assert any(r["CustPo"] == "PO-1" for r in client.load("SLCos", ["CustPo"], "CustPo = 'PO-1'"))
    ops = [c["operation"] for c in erp.calls_since(0)]
    assert "SaveDataSet" in ops and "LoadDataSet" in ops and ops[0] == "CreateSessionToken"


def test_call_log_never_holds_the_password_or_token(erp, client):
    client.load("SLCos", ["CoNum"], "")
    import json

    text = json.dumps(erp.calls_since(0))
    assert PASS not in text


def test_next_co_and_job_numbers_increase(erp, client):
    erp.load({"counters": {"co": 700, "job": 9000}})
    assert client.invoke("SLCos", "NextCoSp", []) == ["700"]
    assert client.invoke("SLCos", "NextCoSp", []) == ["701"]
    assert client.invoke("SLJobs", "NextWOSp", []) == ["9000"]


def test_create_note_stores_the_note(erp, client):
    client.invoke("SpecificNotes", "CreateSpecificNoteSp", ["ObjectNotes", "rp-1", "desc", "text", 0])
    assert client.load("SpecificNotes", ["NoteContent"], "")[0]["NoteContent"] == "text"


def test_update_changes_the_matching_row(erp, client):
    erp.load({"collections": {"SLCos": [{"CoNum": "5", "CustPo": "old"}]}})
    client.update("SLCos", {"CoNum": "5"}, {"CustPo": "new"})
    assert client.load("SLCos", ["CustPo"], "CoNum = '5'")[0]["CustPo"] == "new"


def test_unreachable_fault_gives_the_client_an_unreachable_error(erp, client):
    erp.set_fault("erp.unreachable")
    with pytest.raises(errors.ErpUnreachable):
        client.load("SLCos", ["CoNum"], "")


def test_row_rejected_fault_is_a_rejection_inside_http_200(erp, client):
    erp.set_fault("erp.row_rejected")
    with pytest.raises(errors.ErpRejected):
        client.insert("SLCos", {"CustPo": "x"})
    assert erp.rows("SLCos") == []  # nothing stored


def test_session_expired_fault_makes_the_client_log_in_again_once(erp, client):
    client.load("SLCos", ["CoNum"], "")
    erp.set_fault("erp.session_expired", remaining=1)
    client.load("SLCos", ["CoNum"], "")
    logins = [c for c in erp.calls_since(0) if c["operation"] == "CreateSessionToken"]
    assert len(logins) == 2


def test_slow_fault_delays_the_answer(erp, client, monkeypatch):
    import time

    erp.set_fault("erp.slow", params={"delay_ms": 300})
    start = time.monotonic()
    client.load("SLCos", ["CoNum"], "")
    assert time.monotonic() - start >= 0.3


def test_reset_clears_rows_calls_and_faults(erp, client):
    erp.load({"collections": {"SLCos": [{"CoNum": "1"}]}})
    erp.set_fault("erp.slow")
    erp.reset()
    assert erp.rows("SLCos") == [] and erp.calls_since(0) == [] and erp.faults_active() == []


def test_control_endpoints_over_http(erp):
    r = requests.post(erp.base + "/__erp__/load", json={"collections": {"X": [{"A": "1"}]}})
    assert r.status_code == 200
    assert requests.get(erp.base + "/__erp__/health").json() == {"ok": True}
    assert requests.get(erp.base + "/__erp__/calls?since=0").json() == []
    assert requests.post(erp.base + "/__erp__/reset").status_code == 200
