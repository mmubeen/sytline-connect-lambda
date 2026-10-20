import pytest

from fakes import FakeSoap
from syteline import pipeline

CFG = {"SYTELINE_DEFAULT_WHSE": "MAIN", "SYTELINE_DEFAULT_ORDTYPE": "J"}
CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]


def req(n=2, **kw):
    r = {"request_ref": "r", "customer": {"name": "ACME"},
         "lines": [{"item": f"I{i}", "quantity": i + 1, "uom": "EA"} for i in range(n)]}
    r.update(kw)
    return r


def run(r=None, soap=None, **kw):
    soap = soap or FakeSoap(customers=CUST)
    return pipeline.run_upload(r or req(), soap, CFG, **kw), soap


def test_happy_path_order_and_readback(live_writes):
    out, soap = run()
    assert out["state"] == "succeeded" and out["co_num"] == "100"
    assert out["lines"] == [{"line": 1, "job": "5000"}, {"line": 2, "job": "5001"}]
    kinds = [(c[0], c[1]) for c in soap.calls if c[0] in ("insert", "invoke")]
    assert kinds == [("invoke", "NextCoSp"), ("insert", "SLCos"),
                     ("invoke", "NextWOSp"), ("insert", "CCT_Coitems"), ("insert", "CCTJobs"),
                     ("invoke", "NextWOSp"), ("insert", "CCT_Coitems"), ("insert", "CCTJobs")]
    first_write = soap.calls.index(("invoke", "NextCoSp"))
    assert all(c == ("load", "SLCustAddrs") or c[0] != "load" for c in soap.calls[:first_write])  # only the lookup precedes
    reads = [c for c in soap.calls[soap.calls.index(("insert", "CCTJobs")):] if c[0] == "load"]
    assert len(reads) == 4  # CO + lines + 2 jobs, after the last insert
    assert soap.rows["CCTJobs"][1]["Item"] == "I1" and soap.rows["CCTJobs"][1]["QtyReleased"] == 2
    assert soap.rows["CCTJobs"][0]["Whse"] == "MAIN" and soap.rows["CCTJobs"][0]["OrdType"] == "J"
    assert soap.rows["SLCos"][0]["CustNum"] == "C1"


def test_request_overrides_defaults_and_teardown_is_written(live_writes):
    r = req(1, warehouse="W9", order_type="S")
    r["lines"][0]["teardown"] = "TD-7"
    out, soap = run(r)
    assert out["state"] == "succeeded"
    assert (soap.rows["CCTJobs"][0]["Whse"], soap.rows["CCTJobs"][0]["OrdType"]) == ("W9", "S")
    assert soap.rows["CCT_Coitems"][0]["Teardown"] == "TD-7"


def test_live_writes_off_stops_after_the_lookup_with_no_write_attempt():
    out, soap = run()
    assert out["state"] == "failed" and out["code"] == "LIVE_WRITES_DISABLED"
    assert out["customer_match"] == {"cust_num": "C1", "cust_seq": 0}
    assert soap.writes() == [] and all(c[0] == "load" for c in soap.calls)


@pytest.mark.parametrize("customers,code", [([], "CUSTOMER_NOT_FOUND"),
                                            (CUST + [{"cust_num": "C9", "cust_seq": 0, "name": "ACME"}], "CUSTOMER_AMBIGUOUS")])
def test_customer_failures_are_definite_and_allocate_nothing(live_writes, customers, code):
    out, soap = run(soap=FakeSoap(customers=customers))
    assert out["state"] == "failed" and out["code"] == code and out["step"] == "resolve_customer"
    assert soap.writes() == []


def test_lookup_failure_is_a_definite_failure(live_writes):
    out, soap = run(soap=FakeSoap(customers=CUST, read_fail=True))
    assert out["state"] == "failed" and out["code"] == "ERP_UNREACHABLE" and soap.writes() == []


def test_progress_callback_records_possibly_written(live_writes):
    seen = []
    run(on_progress=lambda possibly, cust: seen.append(len(possibly)))
    assert seen == [1, 2, 3, 4, 5]  # CO, then line/job per line; number allocations are not rows


def test_time_budget_exhausted_before_any_write_fails_cleanly(live_writes):
    out, soap = run(remaining_ms=lambda: 100)
    assert out["state"] == "failed" and out["code"] == "TIME_BUDGET_EXHAUSTED" and soap.writes() == []


def test_time_budget_exhausted_after_writes_needs_reconciliation(live_writes):
    left = iter([10 ** 9, 10 ** 9, 10 ** 9, 100])  # fine for number_co, create_co, number_job_1; low at create_line_1
    out, soap = run(remaining_ms=lambda: next(left))
    assert out["state"] == "needs_reconciliation" and out["code"] == "TIME_BUDGET_EXHAUSTED"
    assert {"kind": "co", "key": "100"} in out["possibly_written"]
