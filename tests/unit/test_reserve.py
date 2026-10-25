from concurrent.futures import ThreadPoolExecutor

import pytest

from fakes import DB_CFG, FakeIdp, FakeJobsTable, FakeSoap, FakeSqlServer, po_fields
from syteline import errors, idos, idp, reservations as rs
from syteline.errors import IntegrationError
from syteline.jobs import RefConflict
from syteline.reservations import ReservationStore
from syteline.reserve import run_reserve

CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]


DOC = "batch-1/PO-1.pdf"


@pytest.fixture(autouse=True)
def fake_idp(monkeypatch):
    f = FakeIdp().add(DOC, po_fields(3))
    monkeypatch.setattr(idp, "_client_factory", lambda: f)
    return f


def req(ref="r1", doc=DOC, **kw):
    r = {"request_ref": ref, "confirmed": True, "uploaded_by": "jdoe", "document_id": doc}
    r.update(kw)
    return r


def go(r=None, soap=None, store=None, **kw):
    soap = soap or FakeSoap(customers=CUST)
    store = store or ReservationStore(FakeJobsTable(), clock=lambda: 1000)
    return run_reserve(r or req(), soap, store, "arn", **kw), soap, store


def test_numbers_come_back_in_line_order_and_nothing_else_is_written(live_writes):
    out, soap, store = go()
    item = out["reservation"]
    assert item["document_id"] == DOC and item["line_count"] == 3 and "po_number" not in item
    assert out["duplicate"] is False and "failure" not in out and item["state"] == rs.RESERVED
    assert item["co_num"] == "100"
    assert item["job_numbers"] == [{"line": 1, "job": "5000", "source": "allocated"},
                                   {"line": 2, "job": "5001", "source": "allocated"},
                                   {"line": 3, "job": "5002", "source": "allocated"}]
    assert item["customer_match"] == {"cust_num": "C1", "cust_seq": 0}
    assert [c for c in soap.writes() if c[0] != "invoke"] == []  # no insert, no update: only number routines
    assert soap.calls.count(("invoke", "NextCoSp")) == 1 and soap.calls.count(("invoke", "NextWOSp")) == 3
    assert not any(soap.rows.get(i) for i in ("SLCos", "CCT_Coitems", "CCTJobs"))


@pytest.mark.parametrize("customers,code", [([], errors.CUSTOMER_NOT_FOUND),
                                            (CUST + [{"cust_num": "C2", "cust_seq": 0, "name": "ACME"}],
                                             errors.CUSTOMER_AMBIGUOUS)])
def test_a_customer_that_is_not_one_confident_match_reserves_nothing(live_writes, customers, code):
    out, soap, store = go(soap=FakeSoap(customers=customers))
    assert out["failure"]["code"] == code and out["reservation"]["state"] == rs.FAILED
    assert out["reservation"]["job_numbers"] == [] and "consumed_unused" not in out
    assert soap.writes() == []


def test_live_writes_off_reserves_no_number():
    out, soap, _ = go()
    assert out["failure"]["code"] == errors.LIVE_WRITES_DISABLED and out["failure"]["step"] == "live_writes_check"
    assert soap.writes() == []  # not even an attempted write


def test_same_request_ref_and_payload_returns_the_same_numbers_without_allocating(live_writes):
    first, soap, store = go()
    before = len(soap.writes())
    again, _, _ = go(soap=soap, store=store)
    assert again["duplicate"] is True and again["reservation"]["job_numbers"] == first["reservation"]["job_numbers"]
    assert len(soap.writes()) == before


def test_same_request_ref_with_a_different_request_conflicts(live_writes, fake_idp):
    _, soap, store = go()
    fake_idp.add("batch-1/OTHER.pdf", po_fields(3))
    with pytest.raises(RefConflict):
        go(req(doc="batch-1/OTHER.pdf"), soap=soap, store=store)


def test_erp_failure_midway_reports_the_numbers_already_obtained_as_consumed(live_writes):
    soap = FakeSoap(customers=CUST, scenarios={("invoke", "NextWOSp", 2): "reject"})
    out, _, _ = go(soap=soap)
    assert out["failure"]["code"] == errors.ERP_REJECTED and out["failure"]["step"] == "number_job_2"
    assert out["reservation"]["state"] == rs.FAILED
    assert out["consumed_unused"] == [{"kind": "co", "number": "100"}, {"kind": "job", "line": 1, "number": "5000"}]
    assert out["reservation"]["job_numbers"] == [{"line": 1, "job": "5000", "source": "allocated"}]


def test_uncertain_number_call_is_reported_as_an_erp_failure_not_a_success(live_writes):
    soap = FakeSoap(customers=CUST, scenarios={("invoke", "NextCoSp", 1): "timeout_after"})
    out, _, _ = go(soap=soap)
    assert out["failure"]["code"] == errors.UNCERTAIN_OUTCOME and out["reservation"]["state"] == rs.FAILED


def test_time_budget_exhausted_stops_and_keeps_the_numbers_obtained(live_writes):
    left = iter([10 ** 9, 10 ** 9, 10 ** 9, 100])  # CO, job 1, job 2 fine; before job 3 almost no time left
    out, soap, _ = go(remaining_ms=lambda: next(left))
    assert out["failure"]["code"] == errors.TIME_BUDGET_EXHAUSTED and out["failure"]["step"] == "number_job_3"
    assert [c["number"] for c in out["consumed_unused"]] == ["100", "5000", "5001"]
    assert soap.calls.count(("invoke", "NextWOSp")) == 2


def test_a_store_failure_after_a_number_was_obtained_still_reports_it(live_writes):
    class Flaky(ReservationStore):
        def add_job_number(self, *a):
            raise RuntimeError("table down")

    out, _, _ = go(store=Flaky(FakeJobsTable(), clock=lambda: 1000))
    assert out["failure"]["code"] == errors.STORE_UNAVAILABLE
    assert [c["number"] for c in out["consumed_unused"]] == ["100", "5000"]


def test_twenty_concurrent_reservations_never_share_a_number(live_writes):
    soap = FakeSoap(customers=CUST)
    store = ReservationStore(FakeJobsTable(), clock=lambda: 1000)

    def one(i):
        return run_reserve(req(ref=f"ref-{i}"), soap, store, "arn")["reservation"]

    with ThreadPoolExecutor(20) as pool:
        items = list(pool.map(one, range(20)))
    cos = [i["co_num"] for i in items]
    jobs = [j["job"] for i in items for j in i["job_numbers"]]
    assert len(set(cos)) == 20 and len(jobs) == 60 and len(set(jobs)) == 60


# ---- the PO details come from the IDP document, never from the caller or our table ---------------
def test_the_reserved_line_count_and_customer_come_from_the_document(live_writes, fake_idp):
    fake_idp.add(DOC, po_fields(2, customer_name="ACME"))
    out, soap, _ = go()
    assert [j["line"] for j in out["reservation"]["job_numbers"]] == [1, 2]
    assert soap.calls.count(("invoke", "NextWOSp")) == 2


@pytest.mark.parametrize("setup,code", [
    (lambda f: f.add(DOC, po_fields(3), status="HITL_IN_PROGRESS"), errors.DOCUMENT_NOT_READY),
    (lambda f: f.add(DOC, {"lines": []}), errors.DOCUMENT_INCOMPLETE),
    (lambda f: f.add(DOC, po_fields(3), low={"cust_po"}), errors.DOCUMENT_NEEDS_REVIEW),
    (lambda f: f.add(DOC, po_fields(26)), errors.TOO_MANY_LINES),
    (lambda f: f.docs.pop(DOC), errors.DOCUMENT_NOT_FOUND),
    (lambda f: f.raise_for.update({DOC: RuntimeError("down")}), errors.IDP_UNAVAILABLE)])
def test_an_unusable_document_reserves_nothing_and_records_nothing(live_writes, fake_idp, setup, code):
    setup(fake_idp)
    soap, store = FakeSoap(customers=CUST), ReservationStore(FakeJobsTable(), clock=lambda: 1000)
    with pytest.raises(IntegrationError) as ei:
        run_reserve(req(), soap, store, "arn")
    assert ei.value.err["code"] == code
    assert soap.calls == [] and store._t.items == {}  # not even the customer lookup, no reservation item


def test_reserved_items_hold_no_po_details(live_writes):
    out, _, store = go()
    text = repr(store._t.items)
    for detail in ("ACME", "'PO-1'", "po_number", "quantity", "part_number", "lines", "'A'"):  # (the matched ERP customer number is not PO data)
        assert detail not in text


# ---- numbers from the database (feature 005) -------------------------------------------------------------
@pytest.fixture
def db_numbers(monkeypatch):
    monkeypatch.setenv(idos.NEXT_CO_SETTING, "database")
    monkeypatch.setenv(idos.NEXT_JOB_SETTING, "database")
    return FakeSqlServer().install(monkeypatch)


def test_numbers_come_from_the_database_over_one_connection_and_the_service_is_not_asked(db_numbers, live_writes):
    out, soap, _ = go(cfg=dict(DB_CFG))
    item = out["reservation"]
    assert item["state"] == rs.RESERVED and item["co_num"] == "100"
    assert item["job_numbers"] == [{"line": 1, "job": "5000", "source": "allocated"},
                                   {"line": 2, "job": "5001", "source": "allocated"},
                                   {"line": 3, "job": "5002", "source": "allocated"}]
    assert db_numbers.committed == [100, 5000, 5001, 5002] and db_numbers.opened == db_numbers.closed == 1
    assert [c for c in soap.calls if c[0] in ("invoke", "insert", "update")] == []


def test_an_unknown_database_outcome_is_uncertain_keeps_the_numbers_obtained_and_is_not_retried(monkeypatch, live_writes):
    monkeypatch.setenv(idos.NEXT_CO_SETTING, "database")
    monkeypatch.setenv(idos.NEXT_JOB_SETTING, "database")
    f = FakeSqlServer(scenarios={("batch", 3): "drop"}).install(monkeypatch)
    out, soap, _ = go(cfg=dict(DB_CFG))
    assert out["failure"]["code"] == errors.UNCERTAIN_OUTCOME and out["failure"]["step"] == "number_job_2"
    assert [c["number"] for c in out["consumed_unused"]] == ["100", "5000"]
    assert out["reservation"]["state"] == rs.FAILED and soap.calls.count(("invoke", "NextWOSp")) == 0
    assert f.opened == f.closed == 1 and len([s for s, _ in f.statements if s.startswith("SET NOCOUNT")]) == 3


def test_missing_database_settings_reserve_nothing_and_open_no_connection(db_numbers, live_writes):
    out, soap, _ = go(cfg={})
    assert out["failure"]["code"] == errors.DATABASE_NOT_CONFIGURED and out["failure"]["step"] == "number_co"
    assert "consumed_unused" not in out and db_numbers.connect_kwargs == []


def test_live_writes_off_asks_the_database_for_nothing(db_numbers):
    out, _, _ = go(cfg=dict(DB_CFG))
    assert out["failure"]["code"] == errors.LIVE_WRITES_DISABLED and db_numbers.connect_kwargs == []


def test_a_database_routine_error_reports_consumed_numbers_and_closes_the_connection(monkeypatch, live_writes):
    monkeypatch.setenv(idos.NEXT_CO_SETTING, "database")
    monkeypatch.setenv(idos.NEXT_JOB_SETTING, "database")
    f = FakeSqlServer(scenarios={("batch", 2): "error"}).install(monkeypatch)
    out, _, _ = go(cfg=dict(DB_CFG))
    assert out["failure"]["code"] == errors.ERP_REJECTED and out["failure"]["step"] == "number_job_1"
    assert [c["number"] for c in out["consumed_unused"]] == ["100"] and f.rolled_back == 1
    assert f.opened == f.closed == 1


# ---- the optional job number of a line (FR-006e, research R23, R24) ---------------------------------------
def doc_with_jobs(fake_idp, jobs):
    """A 3-line document where jobs[i] (when not None) is the job number its line carries."""
    lines = [{"part_number": chr(ord("A") + i), "quantity": i + 1, "uom": "EA", **({"job_number": j} if j else {})}
             for i, j in enumerate(jobs)]
    fake_idp.add(DOC, po_fields(0, lines=lines))


def test_a_line_with_a_job_number_keeps_it_and_the_others_are_allocated(live_writes, fake_idp):
    doc_with_jobs(fake_idp, [None, "J-77", None])
    out, soap, _ = go()
    item = out["reservation"]
    assert item["state"] == rs.RESERVED and "failure" not in out
    assert item["job_numbers"] == [{"line": 1, "job": "5000", "source": "allocated"},
                                   {"line": 2, "job": "J-77", "source": "document"},
                                   {"line": 3, "job": "5001", "source": "allocated"}]
    assert soap.calls.count(("invoke", "NextCoSp")) == 1 and soap.calls.count(("invoke", "NextWOSp")) == 2


def test_every_line_supplied_allocates_only_the_co_number(live_writes, fake_idp):
    doc_with_jobs(fake_idp, ["J1", "J2", "J3"])
    out, soap, _ = go()
    assert out["reservation"]["state"] == rs.RESERVED and out["reservation"]["co_num"] == "100"
    assert [j["job"] for j in out["reservation"]["job_numbers"]] == ["J1", "J2", "J3"]
    assert soap.calls.count(("invoke", "NextWOSp")) == 0 and soap.calls.count(("invoke", "NextCoSp")) == 1


def test_live_writes_off_allocates_nothing_even_when_every_job_number_is_supplied(fake_idp):
    doc_with_jobs(fake_idp, ["J1", "J2", "J3"])
    out, soap, _ = go()
    assert out["failure"]["code"] == errors.LIVE_WRITES_DISABLED and soap.writes() == []


def test_a_supplied_job_number_is_never_reported_as_consumed(live_writes, fake_idp):
    doc_with_jobs(fake_idp, ["J1", None, None])
    soap = FakeSoap(customers=CUST, scenarios={("invoke", "NextWOSp", 2): "reject"})
    out, _, _ = go(soap=soap)
    assert out["failure"]["step"] == "number_job_3"
    assert out["consumed_unused"] == [{"kind": "co", "number": "100"}, {"kind": "job", "line": 2, "number": "5000"}]


def test_the_same_job_number_on_two_lines_is_not_checked_here(live_writes, fake_idp):
    doc_with_jobs(fake_idp, ["J1", "J1", None])
    out, _, _ = go()
    assert out["reservation"]["state"] == rs.RESERVED
    assert [j["job"] for j in out["reservation"]["job_numbers"]][:2] == ["J1", "J1"]


# ---- JOB_NUMBER: a fixed job number for tests (deploy.env) ------------------------------------------------------------
def test_a_configured_job_number_is_used_for_every_line_and_nothing_is_allocated(live_writes, monkeypatch):
    monkeypatch.setenv("JOB_NUMBER", "TEST-001")
    out, soap, _ = go()
    jobs = out["reservation"]["job_numbers"]
    assert [(j["job"], j["source"]) for j in jobs] == [("TEST-001", "configured")] * 3
    assert soap.calls.count(("invoke", "NextWOSp")) == 0 and soap.calls.count(("invoke", "NextCoSp")) == 1


def test_a_configured_job_number_wins_over_the_documents_own(live_writes, monkeypatch, fake_idp):
    doc_with_jobs(fake_idp, [None, "J-77", None])
    monkeypatch.setenv("JOB_NUMBER", "TEST-001")
    out, soap, _ = go()
    assert [j["job"] for j in out["reservation"]["job_numbers"]] == ["TEST-001"] * 3
    assert soap.calls.count(("invoke", "NextWOSp")) == 0


def test_an_empty_job_number_setting_changes_nothing(live_writes, monkeypatch):
    monkeypatch.setenv("JOB_NUMBER", "  ")
    out, soap, _ = go()
    assert [j["source"] for j in out["reservation"]["job_numbers"]] == ["allocated"] * 3
    assert soap.calls.count(("invoke", "NextWOSp")) == 3
