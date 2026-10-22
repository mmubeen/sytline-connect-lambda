import json

import pytest

from fakes import FakeJobsTable, FakeLambda, FakeSoap
from handlers import sweeper, worker
from syteline import jobs
from syteline.jobs import JobStore
from syteline.reservations import ReservationStore

CFG = {"SYTELINE_DEFAULT_WHSE": "M", "SYTELINE_DEFAULT_ORDTYPE": "J"}
REQ = {"request_ref": "r", "confirmed": True, "uploaded_by": "u", "customer": {"name": "ACME"},
       "lines": [{"item": "A", "quantity": 1, "uom": "EA"}]}
CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]


@pytest.fixture
def env(monkeypatch):
    now = [1000]
    store = JobStore(FakeJobsTable(), clock=lambda: now[0])
    soap = FakeSoap(customers=CUST)
    monkeypatch.setattr(worker, "_store_factory", lambda: store)
    monkeypatch.setattr(worker, "_clients_factory", lambda: (soap, CFG))
    lam = FakeLambda()
    monkeypatch.setattr(sweeper, "_store_factory", lambda: store)
    monkeypatch.setattr(sweeper, "_lambda_factory", lambda: lam)
    monkeypatch.setattr(sweeper, "_reservation_store_factory", lambda: ReservationStore(FakeJobsTable()))
    monkeypatch.setattr(worker, "_reservation_store_factory", lambda: ReservationStore(FakeJobsTable()))
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "w")
    return store, soap, lam, now


def new_job(store):
    return store.create_job(REQ, "h", "u", "arn")[0]["upload_id"]


def test_worker_success_persists_result(env, live_writes):
    store, soap, _, _ = env
    jid = new_job(store)
    assert worker.handler({"upload_id": jid}, None) == {"state": "succeeded"}
    j = store.get_job(jid)
    assert j["state"] == "succeeded" and j["erp_records"]["co_num"] == "100" and j["attempts"] == 1
    assert j["customer_match"] == {"cust_num": "C1", "cust_seq": 0}


def test_duplicate_invocation_never_reruns_an_upload(env, live_writes):
    store, soap, _, _ = env
    jid = new_job(store)
    worker.handler({"upload_id": jid}, None)
    inserts = len([c for c in soap.calls if c[0] == "insert"])
    assert worker.handler({"upload_id": jid}, None) == {"skipped": True}
    assert len([c for c in soap.calls if c[0] == "insert"]) == inserts
    assert soap.calls.count(("invoke", "NextCoSp")) == 1


def test_worker_maps_failed_and_needs_reconciliation(env, live_writes):
    store, soap, _, _ = env
    soap.scenarios[("insert", "CCT_Coitems", 1)] = "timeout_after"
    jid = new_job(store)
    worker.handler({"upload_id": jid}, None)
    j = store.get_job(jid)
    assert j["state"] == "needs_reconciliation" and j["failure"]["code"] == "UNCERTAIN_OUTCOME"
    assert {"kind": "co", "key": "100"} in j["erp_records"]["possibly_written"]


def test_worker_live_writes_off_marks_failed_and_writes_nothing(env):
    store, soap, _, _ = env
    jid = new_job(store)
    worker.handler({"upload_id": jid}, None)
    j = store.get_job(jid)
    assert j["state"] == "failed" and j["failure"]["code"] == "LIVE_WRITES_DISABLED"
    assert soap.writes() == []


def test_unexpected_exception_after_writes_is_parked_not_retried(env, live_writes, monkeypatch):
    store, soap, _, _ = env
    real = soap.insert

    def boom(ido, values):
        if ido == "CCT_Coitems":
            raise ValueError("bug")
        return real(ido, values)

    soap.insert = boom
    jid = new_job(store)
    worker.handler({"upload_id": jid}, None)
    j = store.get_job(jid)
    assert j["state"] == "needs_reconciliation" and j["failure"]["code"] == "UNCERTAIN_OUTCOME"


def test_sweeper_redrives_only_old_accepted_jobs(env):
    store, _, lam, now = env
    a = new_job(store)
    now[0] += 200
    fresh = store.create_job({**REQ, "request_ref": "r2"}, "h2", "u", "arn")[0]["upload_id"]
    out = sweeper.handler({}, None)
    assert out["redriven"] == [a] and json.loads(lam.invocations[0][2]) == {"upload_id": a}
    assert store.get_job(a)["state"] == "accepted" and store.get_job(fresh)["state"] == "accepted"


def test_sweeper_parks_stale_running_and_never_redrives_it(env, monkeypatch):
    store, _, lam, now = env
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
    store, _, lam, now = env
    jid = new_job(store)
    store.claim(jid)
    store.park(jid, "X", "m", "s", [])
    now[0] += 100000
    assert sweeper.handler({}, None) == {"redriven": [], "parked": []} and lam.invocations == []


# ---- job-number workflow: the save run (feature 004, PO details read from the IDP document) ------
from fakes import FakeIdp, po_fields  # noqa: E402
from syteline import idos, idp, validation  # noqa: E402

DOC = "batch-1/PO-1.pdf"
FP = validation.fingerprint({"name": "ACME"}, "PO-1", 2)


@pytest.fixture
def renv(monkeypatch):
    now = [1000]
    rstore = ReservationStore(FakeJobsTable(), clock=lambda: now[0])
    soap = FakeSoap(customers=CUST)
    fake_idp = FakeIdp().add(DOC, po_fields(2))
    monkeypatch.setattr(idp, "_client_factory", lambda: fake_idp)
    monkeypatch.setattr(worker, "_reservation_store_factory", lambda: rstore)
    monkeypatch.setattr(worker, "_clients_factory", lambda: (soap, CFG))
    lam = FakeLambda()
    monkeypatch.setattr(sweeper, "_reservation_store_factory", lambda: rstore)
    monkeypatch.setattr(sweeper, "_store_factory", lambda: JobStore(FakeJobsTable()))
    monkeypatch.setattr(sweeper, "_lambda_factory", lambda: lam)
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "w")
    rstore.fake_idp = fake_idp
    return rstore, soap, lam, now


def accepted(rstore, ref="r1", params=None):
    item, _ = rstore.create_reservation({"request_ref": ref, "document_id": DOC, "line_count": 2}, "h", FP, "u", "arn")
    rid = item["reservation_id"]
    rstore.set_customer(rid, {"cust_num": "C1", "cust_seq": 0})
    rstore.set_co_num(rid, "100")
    rstore.add_job_number(rid, [{"line": 1, "job": "5000"}, {"line": 2, "job": "5001"}])
    rstore.mark_reserved(rid)
    rstore.accept_save(rid, params or {"document_id": DOC})
    return rid


def test_save_worker_success_persists_per_line_results(renv, live_writes):
    rstore, soap, _, _ = renv
    rid = accepted(rstore)
    assert worker.handler({"reservation_id": rid}, None) == {"state": "saved"}
    item = rstore.get(rid)
    assert item["state"] == "saved" and item["attempts"] == 1
    assert [r["status"] for r in item["results"]] == ["saved", "saved"]
    assert not any(c == ("invoke", "NextWOSp") for c in soap.calls)


def test_a_duplicate_save_invocation_never_reruns_it(renv, live_writes):
    rstore, soap, _, _ = renv
    rid = accepted(rstore)
    worker.handler({"reservation_id": rid}, None)
    writes = len(soap.writes())
    assert worker.handler({"reservation_id": rid}, None) == {"skipped": True}
    assert len(soap.writes()) == writes


def test_save_worker_maps_failed_and_needs_reconciliation(renv, live_writes):
    rstore, soap, _, _ = renv
    soap.scenarios[("insert", "CCT_Coitems", 1)] = "timeout_after"
    rid = accepted(rstore)
    worker.handler({"reservation_id": rid}, None)
    item = rstore.get(rid)
    assert item["state"] == "needs_reconciliation" and item["failure"]["code"] == "UNCERTAIN_OUTCOME"
    assert {"kind": "co", "key": "100"} in item["erp_records"]["possibly_written"]


def test_save_worker_live_writes_off_is_a_clean_failure(renv):
    rstore, soap, _, _ = renv
    rid = accepted(rstore)
    assert worker.handler({"reservation_id": rid}, None) == {"state": "failed"}
    assert rstore.get(rid)["failure"]["code"] == "LIVE_WRITES_DISABLED" and soap.writes() == []


def test_the_worker_writes_what_the_document_says_now_not_a_stored_copy(renv, live_writes):
    rstore, soap, _, _ = renv
    rid = accepted(rstore, params={"document_id": DOC, "warehouse": "W9", "order_type": "S", "order_date": "2026-10-20"})
    corrected = po_fields(2)
    corrected["lines"][1]["quantity"] = 7  # a line corrected in review after the save was accepted
    rstore.fake_idp.add(DOC, corrected)
    assert worker.handler({"reservation_id": rid}, None) == {"state": "saved"}
    assert soap.rows["CCT_Coitems"][1]["QtyOrdered"] == 7.0
    assert soap.rows["SLCos"][0]["OrderDate"] == "2026-10-20"
    assert soap.rows["CCTJobs"][0]["Whse"] == "W9" and soap.rows["CCTJobs"][0]["OrdType"] == "S"
    assert [c for c in soap.calls if c[0] == "invoke"] == []


def test_a_document_that_changed_identity_fails_the_save_with_nothing_written(renv, live_writes):
    rstore, soap, _, _ = renv
    rid = accepted(rstore)
    rstore.fake_idp.add(DOC, po_fields(2, cust_po="PO-2"))
    assert worker.handler({"reservation_id": rid}, None) == {"state": "failed"}
    item = rstore.get(rid)
    assert item["failure"]["code"] == "DOCUMENT_CHANGED" and soap.writes() == []


@pytest.mark.parametrize("change,code", [
    (lambda f: f.update(DOC, status="HITL_IN_PROGRESS"), "DOCUMENT_NOT_READY"),
    (lambda f: f.add(DOC, po_fields(2), low={"cust_po"}), "DOCUMENT_NEEDS_REVIEW"),
    (lambda f: f.docs.pop(DOC), "DOCUMENT_NOT_FOUND")])
def test_a_document_that_became_unusable_fails_the_save_with_nothing_written(renv, live_writes, change, code):
    rstore, soap, _, _ = renv
    rid = accepted(rstore)
    change(rstore.fake_idp)
    assert worker.handler({"reservation_id": rid}, None) == {"state": "failed"}
    assert rstore.get(rid)["failure"]["code"] == code and soap.writes() == []


def test_an_unavailable_idp_source_is_retried_later_and_gives_up_after_five_attempts(renv, live_writes):
    rstore, soap, lam, now = renv
    rid = accepted(rstore)
    rstore.fake_idp.raise_for[DOC] = RuntimeError("down")
    for attempt in range(1, 5):
        assert worker.handler({"reservation_id": rid}, None) == {"state": "save_accepted"}
        assert rstore.get(rid)["state"] == "save_accepted" and rstore.get(rid)["attempts"] == attempt
    assert worker.handler({"reservation_id": rid}, None) == {"state": "failed"}  # the 5th attempt gives up
    assert rstore.get(rid)["failure"]["code"] == "IDP_UNAVAILABLE" and soap.writes() == []


def test_the_sweeper_starts_a_released_save_again(renv, live_writes):
    rstore, soap, lam, now = renv
    rid = accepted(rstore)
    rstore.fake_idp.raise_for[DOC] = RuntimeError("down")
    worker.handler({"reservation_id": rid}, None)
    now[0] = 1300
    assert sweeper.handler({}, None)["redriven"] == [rid]
    rstore.fake_idp.raise_for.clear()
    assert worker.handler({"reservation_id": rid}, None) == {"state": "saved"}


def test_save_worker_unexpected_error_never_retries(renv, live_writes, monkeypatch):
    rstore, soap, _, _ = renv
    rid = accepted(rstore)
    monkeypatch.setattr("syteline.savewf.run_save", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert worker.handler({"reservation_id": rid}, None) == {"state": "failed"}  # nothing was possibly written
    assert rstore.get(rid)["state"] == "failed"
    assert worker.handler({"reservation_id": rid}, None) == {"skipped": True}


def test_sweeper_redrives_only_old_save_accepted_reservations(renv):
    rstore, _, lam, now = renv
    old = accepted(rstore, ref="old")
    now[0] = 1150
    new = accepted(rstore, ref="new")
    reserved_only = rstore.create_reservation({"request_ref": "z", "document_id": DOC, "line_count": 1}, "h", "f", "u", "a")[0]
    rstore.mark_reserved(reserved_only["reservation_id"])  # reserved but never saved: never driven
    now[0] = 1200
    out = sweeper.handler({}, None)
    assert out["redriven"] == [old] and out["parked"] == []
    assert [json.loads(p) for _, _, p in lam.invocations] == [{"reservation_id": old}]
    assert rstore.get(old)["state"] == "save_accepted" and rstore.get(new)["state"] == "save_accepted"


def test_sweeper_parks_stale_saving_and_never_redrives_it(renv):
    rstore, _, lam, now = renv
    rid = accepted(rstore)
    rstore.claim(rid)
    rstore.record_progress(rid, [{"kind": "co", "key": "100"}], [{"line": 1, "status": "not_attempted"}])
    now[0] = 1000 + 400
    out = sweeper.handler({}, None)
    assert out["parked"] == [rid] and out["redriven"] == [] and lam.invocations == []
    item = rstore.get(rid)
    assert item["state"] == "needs_reconciliation" and item["failure"]["code"] == "STALE_RUNNING"
    assert item["erp_records"]["possibly_written"] == [{"kind": "co", "key": "100"}]


def test_sweeper_leaves_finished_reservations_alone(renv, live_writes):
    rstore, soap, lam, now = renv
    done, parked = accepted(rstore, ref="a"), accepted(rstore, ref="b")
    worker.handler({"reservation_id": done}, None)
    soap.scenarios[("insert", "SLCos", 2)] = "timeout_after"
    worker.handler({"reservation_id": parked}, None)
    assert rstore.get(done)["state"] == "saved" and rstore.get(parked)["state"] == "needs_reconciliation"
    now[0] = 10 ** 6
    out = sweeper.handler({}, None)
    assert out == {"redriven": [], "parked": []} and lam.invocations == []
    assert rstore.get(parked)["state"] == "needs_reconciliation"


def test_the_save_worker_stores_the_order_level_warnings(renv, live_writes, monkeypatch):
    rstore, soap, _, _ = renv
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "database")  # no database settings in the worker's config: a warning only
    rid = accepted(rstore)
    assert worker.handler({"reservation_id": rid}, None) == {"state": "saved"}
    item = rstore.get(rid)
    assert item["state"] == "saved" and len(item["warnings"]) == 1
    assert item["warnings"][0].startswith("customer defaults not available:")
