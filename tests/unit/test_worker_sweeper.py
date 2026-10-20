import json

import pytest

from fakes import FakeJobsTable, FakeLambda, FakeSoap, FakeSql
from handlers import sweeper, worker
from syteline import jobs
from syteline.jobs import JobStore

CFG = {"SYTELINE_DEFAULT_WHSE": "M", "SYTELINE_DEFAULT_ORDTYPE": "J"}
REQ = {"request_ref": "r", "confirmed": True, "uploaded_by": "u", "customer": {"name": "ACME"},
       "lines": [{"item": "A", "quantity": 1, "uom": "EA"}]}
CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]


@pytest.fixture
def env(monkeypatch):
    now = [1000]
    store = JobStore(FakeJobsTable(), clock=lambda: now[0])
    sql, soap = FakeSql(customers=CUST), FakeSoap()
    monkeypatch.setattr(worker, "_store_factory", lambda: store)
    monkeypatch.setattr(worker, "_clients_factory", lambda: (sql, soap, CFG))
    lam = FakeLambda()
    monkeypatch.setattr(sweeper, "_store_factory", lambda: store)
    monkeypatch.setattr(sweeper, "_lambda_factory", lambda: lam)
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "w")
    return store, sql, soap, lam, now


def new_job(store):
    return store.create_job(REQ, "h", "u", "arn")[0]["job_id"]


def test_worker_success_persists_result(env, live_writes):
    store, sql, soap, _, _ = env
    jid = new_job(store)
    assert worker.handler({"job_id": jid}, None) == {"state": "succeeded"}
    j = store.get_job(jid)
    assert j["state"] == "succeeded" and j["erp_records"]["co_num"] == "100" and j["attempts"] == 1
    assert j["customer_match"] == {"cust_num": "C1", "cust_seq": 0}


def test_duplicate_invocation_never_reruns_an_upload(env, live_writes):
    store, sql, soap, _, _ = env
    jid = new_job(store)
    worker.handler({"job_id": jid}, None)
    inserts = len([c for c in soap.calls if c[0] == "insert"])
    assert worker.handler({"job_id": jid}, None) == {"skipped": True}
    assert len([c for c in soap.calls if c[0] == "insert"]) == inserts and sql.allocations.count("co") == 1


def test_worker_maps_failed_and_needs_reconciliation(env, live_writes):
    store, sql, soap, _, _ = env
    soap.scenarios[("insert", "CCT_Coitems", 1)] = "timeout_after"
    jid = new_job(store)
    worker.handler({"job_id": jid}, None)
    j = store.get_job(jid)
    assert j["state"] == "needs_reconciliation" and j["failure"]["code"] == "UNCERTAIN_OUTCOME"
    assert {"kind": "co", "key": "100"} in j["erp_records"]["possibly_written"]


def test_worker_live_writes_off_marks_failed_and_writes_nothing(env):
    store, sql, soap, _, _ = env
    jid = new_job(store)
    worker.handler({"job_id": jid}, None)
    j = store.get_job(jid)
    assert j["state"] == "failed" and j["failure"]["code"] == "LIVE_WRITES_DISABLED"
    assert sql.allocations == [] and soap.calls == []


def test_unexpected_exception_after_writes_is_parked_not_retried(env, live_writes, monkeypatch):
    store, sql, soap, _, _ = env
    real = soap.insert

    def boom(ido, values):
        if ido == "CCT_Coitems":
            raise ValueError("bug")
        return real(ido, values)

    soap.insert = boom
    jid = new_job(store)
    worker.handler({"job_id": jid}, None)
    j = store.get_job(jid)
    assert j["state"] == "needs_reconciliation" and j["failure"]["code"] == "UNCERTAIN_OUTCOME"


def test_sweeper_redrives_only_old_accepted_jobs(env):
    store, _, _, lam, now = env
    a = new_job(store)
    now[0] += 200
    fresh = store.create_job({**REQ, "request_ref": "r2"}, "h2", "u", "arn")[0]["job_id"]
    out = sweeper.handler({}, None)
    assert out["redriven"] == [a] and json.loads(lam.invocations[0][2]) == {"job_id": a}
    assert store.get_job(a)["state"] == "accepted" and store.get_job(fresh)["state"] == "accepted"


def test_sweeper_parks_stale_running_and_never_redrives_it(env, monkeypatch):
    store, _, _, lam, now = env
    jid = new_job(store)
    store.claim(jid)
    store.record_progress(jid, [{"kind": "co", "key": "100"}], {"cust_num": "C1"})
    now[0] += 400
    out = sweeper.handler({}, None)
    j = store.get_job(jid)
    assert out["parked"] == [jid] and out["redriven"] == []
    assert j["state"] == "needs_reconciliation" and j["failure"]["code"] == "STALE_RUNNING"
    assert j["erp_records"]["possibly_written"] == [{"kind": "co", "key": "100"}] and lam.invocations == []
    now[0] += 10000
    assert sweeper.handler({}, None) == {"redriven": [], "parked": []}


def test_sweeper_leaves_needs_reconciliation_alone(env):
    store, _, _, lam, now = env
    jid = new_job(store)
    store.claim(jid)
    store.park(jid, "X", "m", "s", [])
    now[0] += 100000
    assert sweeper.handler({}, None) == {"redriven": [], "parked": []} and lam.invocations == []
