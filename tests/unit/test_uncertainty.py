"""Classification of failures (research.md R6, FR-004): never delete, never retry."""

import pytest

from fakes import FakeSoap
from syteline import pipeline

CFG = {"SYTELINE_DEFAULT_WHSE": "M", "SYTELINE_DEFAULT_ORDTYPE": "J"}
CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]


def req(n=2):
    return {"request_ref": "r", "customer": {"name": "ACME"},
            "lines": [{"item": f"I{i}", "quantity": i + 1, "uom": "EA"} for i in range(n)]}


@pytest.fixture(autouse=True)
def _on(live_writes):
    pass


def go(scenarios=None, n=2, **kw):
    soap = FakeSoap(scenarios, customers=CUST, **kw)
    return pipeline.run_upload(req(n), soap, CFG), soap


def attempts(soap, kind, name):
    return len([c for c in soap.calls if c == (kind, name)])


def test_first_insert_rejected_and_readback_proves_absent_is_failed():
    out, soap = go({("insert", "SLCos", 1): "reject"})
    assert out["state"] == "failed" and out["code"] == "ERP_REJECTED" and out["step"] == "create_co"
    assert soap.count("SLCos") == 0 and attempts(soap, "insert", "SLCos") == 1  # one attempt only


def test_first_insert_rejected_but_readback_fails_is_needs_reconciliation():
    out, _ = go({("insert", "SLCos", 1): "reject"}, readback_fail=True)
    assert out["state"] == "needs_reconciliation" and out["possibly_written"] == [{"kind": "co", "key": "100"}]


def test_first_insert_not_even_connected_is_failed():
    out, _ = go({("insert", "SLCos", 1): "unreachable"})
    assert out["state"] == "failed" and out["code"] == "ERP_UNREACHABLE"


def test_timeout_after_insert_is_never_failed_even_if_row_is_visible():
    out, soap = go({("insert", "SLCos", 1): "timeout_after"})
    assert out["state"] == "needs_reconciliation" and out["code"] == "UNCERTAIN_OUTCOME"
    assert soap.count("SLCos") == 1 and out["possibly_written"] == [{"kind": "co", "key": "100"}]
    assert attempts(soap, "insert", "SLCos") == 1 and not hasattr(soap, "delete")


def test_timeout_before_storing_still_needs_reconciliation():
    out, _ = go({("insert", "SLCos", 1): "timeout_before"})
    assert out["state"] == "needs_reconciliation"  # a timeout never proves the row is absent


def test_timeout_on_later_line_lists_everything_that_may_exist():
    out, soap = go({("insert", "SLCoitems", 2): "timeout_after"})
    assert out["state"] == "needs_reconciliation" and out["step"] == "create_line_2"
    assert out["possibly_written"] == [
        {"kind": "co", "key": "100"}, {"kind": "line", "key": "100/1"}, {"kind": "job", "key": "5000"},
        {"kind": "line", "key": "100/2"}]
    assert len([c for c in soap.calls if c[0] == "insert"]) == 4  # nothing retried


def test_rejection_after_earlier_rows_is_needs_reconciliation_not_failed():
    out, soap = go({("insert", "CCTJobs", 1): "reject"})
    assert out["state"] == "needs_reconciliation" and out["code"] == "ERP_REJECTED"
    assert soap.count("SLCos") == 1 and soap.count("SLCoitems") == 1  # left in place for a person


def test_unreachable_after_earlier_rows_is_needs_reconciliation():
    out, _ = go({("insert", "SLCoitems", 1): "unreachable"})
    assert out["state"] == "needs_reconciliation" and out["possibly_written"] == [{"kind": "co", "key": "100"}]


def test_readback_failure_is_needs_reconciliation_with_all_records():
    out, _ = go(n=1, readback_fail=True)
    assert out["state"] == "needs_reconciliation" and out["step"] == "read_back" and len(out["possibly_written"]) == 3


@pytest.mark.parametrize("corrupt", [{("SLCoitems", "QtyOrdered"): "99"}, {("SLCoitems", "Item"): "WRONG"},
                                     {("SLCos", "CustNum"): "OTHER"}])
def test_readback_mismatch_is_needs_reconciliation(corrupt):
    out, _ = go(n=1, corrupt=corrupt)
    assert out["state"] == "needs_reconciliation" and out["code"] == "READBACK_MISMATCH"


def test_number_allocation_failure_before_any_row_is_failed():
    for scenario in ("timeout_before", "timeout_after", "reject", "unreachable"):
        out, soap = go({("invoke", "NextCoSp", 1): scenario})
        assert out["state"] == "failed" and out["step"] == "number_co", scenario
        assert soap.count("SLCos") == 0


def test_number_allocation_failure_after_rows_exist_is_needs_reconciliation():
    out, _ = go({("invoke", "NextWOSp", 1): "timeout_before"})
    assert out["state"] == "needs_reconciliation" and out["step"] == "number_job_1"
    assert out["possibly_written"] == [{"kind": "co", "key": "100"}]  # the CO row exists
