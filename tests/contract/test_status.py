import json

from fakes import FakeJobsTable
from handlers import status
from syteline.jobs import JobStore


def mk(monkeypatch):
    store = JobStore(FakeJobsTable())
    monkeypatch.setattr(status, "_store_factory", lambda: store)
    job, _ = store.create_job({"request_ref": "r"}, "h", "jdoe", "arn")
    return store, job["job_id"]


def get(job_id):
    r = status.handler({"pathParameters": {"id": job_id}}, None)
    return r["statusCode"], json.loads(r["body"])


def test_accepted_and_running_have_no_erp_fields(monkeypatch):
    store, jid = mk(monkeypatch)
    code, out = get(jid)
    assert code == 200 and out["state"] == "accepted" and "co_num" not in out and "failure" not in out
    store.claim(jid)
    assert get(jid)[1]["state"] == "running"


def test_succeeded_has_co_and_jobs(monkeypatch):
    store, jid = mk(monkeypatch)
    store.claim(jid)
    store.succeed(jid, "100", [{"line": 1, "job": "5000"}])
    code, out = get(jid)
    assert out["state"] == "succeeded" and out["co_num"] == "100" and out["jobs"] == [{"line": 1, "job": "5000"}]
    assert out["uploaded_by"] == "jdoe" and "possibly_written" not in out


def test_failed_has_failure_only(monkeypatch):
    store, jid = mk(monkeypatch)
    store.claim(jid)
    store.fail(jid, "CUSTOMER_NOT_FOUND", "no match", "resolve_customer")
    out = get(jid)[1]
    assert out["failure"] == {"code": "CUSTOMER_NOT_FOUND", "message": "no match", "step": "resolve_customer"}
    assert "co_num" not in out and "possibly_written" not in out


def test_needs_reconciliation_lists_possibly_written(monkeypatch):
    store, jid = mk(monkeypatch)
    store.claim(jid)
    store.park(jid, "UNCERTAIN_OUTCOME", "timeout", "create_co", [{"kind": "co", "key": "100"}])
    out = get(jid)[1]
    assert out["state"] == "needs_reconciliation" and out["possibly_written"] == [{"kind": "co", "key": "100"}]
    assert "co_num" not in out and out["failure"]["code"] == "UNCERTAIN_OUTCOME"


def test_404(monkeypatch):
    mk(monkeypatch)
    code, out = get("does-not-exist")
    assert code == 404 and out["errors"][0]["code"] == "NOT_FOUND"
