from concurrent.futures import ThreadPoolExecutor

import pytest

from fakes import FakeJobsTable, FakeSoap
from syteline import errors, reservations as rs
from syteline.jobs import RefConflict
from syteline.reservations import ReservationStore
from syteline.reserve import run_reserve

CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]


def req(n=3, ref="r1", **kw):
    r = {"request_ref": ref, "confirmed": True, "uploaded_by": "jdoe", "customer": {"name": "ACME"},
         "po_number": "PO-1", "line_count": n}
    r.update(kw)
    return r


def go(r=None, soap=None, store=None, **kw):
    soap = soap or FakeSoap(customers=CUST)
    store = store or ReservationStore(FakeJobsTable(), clock=lambda: 1000)
    return run_reserve(r or req(), soap, store, "arn", **kw), soap, store


def test_numbers_come_back_in_line_order_and_nothing_else_is_written(live_writes):
    out, soap, store = go()
    item = out["reservation"]
    assert out["duplicate"] is False and "failure" not in out and item["state"] == rs.RESERVED
    assert item["co_num"] == "100"
    assert item["job_numbers"] == [{"line": 1, "job": "5000"}, {"line": 2, "job": "5001"}, {"line": 3, "job": "5002"}]
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


def test_same_request_ref_with_a_different_request_conflicts(live_writes):
    _, soap, store = go()
    with pytest.raises(RefConflict):
        go(req(po_number="PO-2"), soap=soap, store=store)


def test_erp_failure_midway_reports_the_numbers_already_obtained_as_consumed(live_writes):
    soap = FakeSoap(customers=CUST, scenarios={("invoke", "NextWOSp", 2): "reject"})
    out, _, _ = go(soap=soap)
    assert out["failure"]["code"] == errors.ERP_REJECTED and out["failure"]["step"] == "number_job_2"
    assert out["reservation"]["state"] == rs.FAILED
    assert out["consumed_unused"] == [{"kind": "co", "number": "100"}, {"kind": "job", "line": 1, "number": "5000"}]
    assert out["reservation"]["job_numbers"] == [{"line": 1, "job": "5000"}]


def test_uncertain_number_call_is_reported_as_an_erp_failure_not_a_success(live_writes):
    soap = FakeSoap(customers=CUST, scenarios={("invoke", "NextCoSp", 1): "timeout_after"})
    out, _, _ = go(soap=soap)
    assert out["failure"]["code"] == errors.ERP_UNREACHABLE and out["reservation"]["state"] == rs.FAILED


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
        return run_reserve(req(n=2, ref=f"ref-{i}"), soap, store, "arn")["reservation"]

    with ThreadPoolExecutor(20) as pool:
        items = list(pool.map(one, range(20)))
    cos = [i["co_num"] for i in items]
    jobs = [j["job"] for i in items for j in i["job_numbers"]]
    assert len(set(cos)) == 20 and len(jobs) == 40 and len(set(jobs)) == 40
