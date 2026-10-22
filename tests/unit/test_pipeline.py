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


# ---- numbers from the database (feature 005) -------------------------------------------------------------
from fakes import DB_CFG, FakeSqlServer  # noqa: E402
from syteline import idos  # noqa: E402


@pytest.fixture
def db_numbers(monkeypatch):
    monkeypatch.setenv(idos.NEXT_CO_SETTING, "database")
    monkeypatch.setenv(idos.NEXT_JOB_SETTING, "database")
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "SLCos")  # keep the customer defaults off the database in these tests


def run_db(r=None, soap=None, **kw):
    soap = soap or FakeSoap(customers=CUST)
    return pipeline.run_upload(r or req(), soap, dict(CFG, **DB_CFG), **kw), soap


def test_the_upload_takes_its_numbers_from_the_database_and_writes_rows_through_the_service(monkeypatch, db_numbers,
                                                                                          live_writes):
    f = FakeSqlServer().install(monkeypatch)
    out, soap = run_db()
    assert out["state"] == "succeeded" and out["co_num"] == "100"
    assert out["lines"] == [{"line": 1, "job": "5000"}, {"line": 2, "job": "5001"}]
    assert f.committed == [100, 5000, 5001] and f.opened == f.closed == 1
    assert [c[1] for c in soap.calls if c[0] == "invoke"] == []  # no numbering call to the web service
    assert [c[1] for c in soap.calls if c[0] == "insert"] == ["SLCos", "CCT_Coitems", "CCTJobs", "CCT_Coitems", "CCTJobs"]


def test_an_unknown_database_outcome_for_the_first_number_is_failed_with_the_uncertain_code(monkeypatch, db_numbers,
                                                                                         live_writes):
    FakeSqlServer(scenarios={("batch", 1): "drop"}).install(monkeypatch)
    out, soap = run_db()
    assert out["state"] == "failed" and out["code"] == "UNCERTAIN_OUTCOME" and out["step"] == "number_co"
    assert soap.count("SLCos") == 0


def test_an_unknown_database_outcome_after_rows_exist_needs_reconciliation(monkeypatch, db_numbers, live_writes):
    FakeSqlServer(scenarios={("batch", 2): "drop"}).install(monkeypatch)
    out, _ = run_db()
    assert out["state"] == "needs_reconciliation" and out["step"] == "number_job_1" and out["code"] == "UNCERTAIN_OUTCOME"
    assert out["possibly_written"] == [{"kind": "co", "key": "100"}]


def test_missing_database_settings_fail_the_upload_before_anything_is_written(monkeypatch, db_numbers, live_writes):
    f = FakeSqlServer().install(monkeypatch)
    out, soap = pipeline.run_upload(req(), FakeSoap(customers=CUST), dict(CFG)), None
    assert out["state"] == "failed" and out["code"] == "DATABASE_NOT_CONFIGURED" and f.connect_kwargs == []


def test_the_database_connection_is_closed_when_the_upload_ends_in_any_state(monkeypatch, db_numbers, live_writes):
    f = FakeSqlServer().install(monkeypatch)
    soap = FakeSoap(customers=CUST, scenarios={("insert", "CCT_Coitems", 1): "timeout_after"})
    out, _ = run_db(soap=soap)
    assert out["state"] == "needs_reconciliation" and f.opened == f.closed == 1


def test_the_upload_header_carries_the_customers_defaults(monkeypatch, live_writes):
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "database")
    FakeSqlServer(co_rows=[{"co_num": 7, "cust_num": "C1", "cust_seq": 0, "terms_code": "N30",
                            "ship_code": "UPS"}]).install(monkeypatch)
    out, soap = run_db()
    head = soap.rows["SLCos"][0]
    assert out["state"] == "succeeded" and head["TermsCode"] == "N30" and head["ShipCode"] == "UPS"


def test_an_unreadable_default_never_blocks_the_upload(monkeypatch, live_writes):
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "database")
    FakeSqlServer(connect_error="login_fail").install(monkeypatch)
    out, soap = run_db()
    assert out["state"] == "succeeded" and "TermsCode" not in soap.rows["SLCos"][0]
