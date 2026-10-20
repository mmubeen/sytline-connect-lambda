"""Classification of failures (research.md R6, FR-004): never delete, never retry."""

import pytest

from fakes import FakeSoap, FakeSql
from syteline import pipeline

CFG = {"SYTELINE_DEFAULT_WHSE": "M", "SYTELINE_DEFAULT_ORDTYPE": "J"}
CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]


def req(n=2):
    return {"request_ref": "r", "customer": {"name": "ACME"},
            "lines": [{"item": f"I{i}", "quantity": i + 1, "uom": "EA"} for i in range(n)]}


@pytest.fixture(autouse=True)
def _on(live_writes):
    pass


def go(soap, n=2, sql=None):
    return pipeline.run_upload(req(n), sql or FakeSql(customers=CUST), soap, CFG), soap


def assert_no_delete_no_retry(soap, max_inserts):
    assert all(c[0] != "delete" for c in soap.calls) and not hasattr(soap, "delete")
    inserts = [c for c in soap.calls if c[0] == "insert"]
    assert len(inserts) == len(set(enumerate(inserts))) and len(inserts) <= max_inserts


def test_first_insert_rejected_and_readback_proves_absent_is_failed():
    out, soap = go(FakeSoap({("insert", "SLCos", 1): "reject"}))
    assert out["state"] == "failed" and out["code"] == "ERP_REJECTED" and out["step"] == "create_co"
    assert soap.count("SLCos") == 0
    assert [c for c in soap.calls if c[0] == "insert"] == [("insert", "SLCos")]  # one attempt only


def test_first_insert_rejected_but_readback_fails_is_needs_reconciliation():
    out, soap = go(FakeSoap({("insert", "SLCos", 1): "reject"}, read_fail=True))
    assert out["state"] == "needs_reconciliation" and out["possibly_written"] == [{"kind": "co", "key": "100"}]


def test_first_insert_not_even_connected_is_failed():
    out, _ = go(FakeSoap({("insert", "SLCos", 1): "unreachable"}))
    assert out["state"] == "failed" and out["code"] == "ERP_UNREACHABLE"


def test_timeout_after_insert_is_never_failed_even_if_row_is_visible():
    out, soap = go(FakeSoap({("insert", "SLCos", 1): "timeout_after"}))
    assert out["state"] == "needs_reconciliation" and out["code"] == "UNCERTAIN_OUTCOME"
    assert soap.count("SLCos") == 1 and out["possibly_written"] == [{"kind": "co", "key": "100"}]
    assert_no_delete_no_retry(soap, 1)


def test_timeout_before_storing_still_needs_reconciliation():
    out, soap = go(FakeSoap({("insert", "SLCos", 1): "timeout_before"}))
    assert out["state"] == "needs_reconciliation"  # a timeout never proves the row is absent


def test_timeout_on_later_line_lists_everything_that_may_exist():
    out, soap = go(FakeSoap({("insert", "CCT_Coitems", 2): "timeout_after"}))
    assert out["state"] == "needs_reconciliation" and out["step"] == "create_line_2"
    assert out["possibly_written"] == [
        {"kind": "co", "key": "100"}, {"kind": "line", "key": "100/1"}, {"kind": "job", "key": "5000"},
        {"kind": "line", "key": "100/2"}]
    assert_no_delete_no_retry(soap, 4)


def test_rejection_after_earlier_rows_is_needs_reconciliation_not_failed():
    out, soap = go(FakeSoap({("insert", "CCTJobs", 1): "reject"}))
    assert out["state"] == "needs_reconciliation" and out["code"] == "ERP_REJECTED"
    assert soap.count("SLCos") == 1 and soap.count("CCT_Coitems") == 1  # left in place for a person


def test_unreachable_after_earlier_rows_is_needs_reconciliation():
    out, _ = go(FakeSoap({("insert", "CCT_Coitems", 1): "unreachable"}))
    assert out["state"] == "needs_reconciliation" and out["possibly_written"] == [{"kind": "co", "key": "100"}]


def test_readback_failure_is_needs_reconciliation_with_all_records():
    out, _ = go(FakeSoap(read_fail=True), n=1)
    assert out["state"] == "needs_reconciliation" and out["step"] == "read_back"
    assert len(out["possibly_written"]) == 3


@pytest.mark.parametrize("corrupt", [{("CCT_Coitems", "QtyOrdered"): "99"}, {("CCT_Coitems", "Item"): "WRONG"},
                                     {("SLCos", "CustNum"): "OTHER"}])
def test_readback_mismatch_is_needs_reconciliation(corrupt):
    out, _ = go(FakeSoap(corrupt=corrupt), n=1)
    assert out["state"] == "needs_reconciliation" and out["code"] == "READBACK_MISMATCH"


def test_number_allocation_failure_before_rows_is_failed_after_rows_is_parked():
    class SqlBoom(FakeSql):
        def execute_write(self, sql, params=()):
            if "nextwosp" in sql.lower():
                from syteline.errors import ErpUncertain
                raise ErpUncertain("lost connection")
            return super().execute_write(sql, params)

    out, soap = go(FakeSoap(), sql=SqlBoom(customers=CUST))
    assert out["state"] == "needs_reconciliation" and out["step"] == "number_job_1"  # CO row already exists
