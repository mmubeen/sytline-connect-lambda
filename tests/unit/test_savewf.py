import pytest

from fakes import FakeSoap
from syteline import errors, idos, savewf

CFG = {"SYTELINE_DEFAULT_WHSE": "MAIN", "SYTELINE_DEFAULT_ORDTYPE": "J"}
CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]


def reservation(n=2):
    return {"reservation_id": "res-1", "state": "saving", "co_num": "100",
            "job_numbers": [{"line": i, "job": str(5000 + i - 1)} for i in range(1, n + 1)],
            "customer_match": {"cust_num": "C1", "cust_seq": 0}}


def save_req(n=2, **kw):
    r = {"confirmed": True, "uploaded_by": "jdoe", "customer": {"name": "ACME"}, "po_number": "PO-1",
         "lines": [{"item": f"I{i}", "quantity": i + 1, "uom": "EA"} for i in range(n)]}
    r.update(kw)
    return r


def go(n=2, soap=None, req=None, **kw):
    soap = soap or FakeSoap(customers=CUST)
    out = savewf.run_save(reservation(n), req or save_req(n), soap, CFG, **kw)
    return out, soap


def kinds(soap):
    return [(c[0], c[1]) for c in soap.calls if c[0] in ("insert", "update", "invoke")]


def test_happy_path_order_links_and_per_line_results(live_writes):
    req = save_req(2, received_date="2026-10-19", order_date="2026-10-20")
    req["lines"][0].update(teardown="2026-10-25", engine_sn="E1", serial_num="S1", packing="BOX")
    out, soap = go(req=req)
    assert out["state"] == "succeeded" and out["co_num"] == "100"
    assert kinds(soap) == [("insert", "SLCos"), ("insert", "CCT_Coitems"), ("update", "CCT_Coitems"),
                           ("insert", "CCTJobs"), ("insert", "CCT_Coitems"), ("insert", "CCTJobs")]
    assert not any(c == ("invoke", "NextWOSp") or c == ("invoke", "NextCoSp") for c in soap.calls)  # FR-005
    assert out["results"] == [
        {"line": 1, "job": "5000", "item": "I0", "status": "saved", "warnings": [], "co_line": 1},
        {"line": 2, "job": "5001", "item": "I1", "status": "saved", "warnings": [], "co_line": 2}]
    head = soap.rows["SLCos"][0]
    assert head["CustNum"] == "C1" and head[idos.CO_HEADER_PROPS["cust_po"]] == "PO-1"
    assert head["OrderDate"] == "2026-10-20" and "DateReceived" not in head  # SLCos has no received-date property
    line1 = soap.rows["CCT_Coitems"][0]
    assert line1[idos.LINE_LINK_PROPS["ref_num"]] == "5000" and line1[idos.LINE_LINK_PROPS["ref_type"]] == "J"
    assert line1[idos.LINE_TEARDOWN_PROP] == "2026-10-25"  # set by the separate update
    job1 = soap.rows["CCTJobs"][0]
    link = idos.JOB_LINK_PROPS
    assert job1["Job"] == "5000" and job1[link["co_num"]] == "100" and job1[link["co_line"]] == 1 and job1["RootJob"] == "5000"
    assert "CoNum" not in job1 and "CoLine" not in job1  # the job's link back is OrdNum / OrdLine
    assert job1[idos.JOB_EXTRA_PROPS["engine_sn"]] == "E1" and idos.JOB_EXTRA_PROPS["engine_sn"] == "uf_eng_sn" and job1["Whse"] == "MAIN" and job1["OrdType"] == "J"
    assert soap.rows["CCTJobs"][1]["Job"] == "5001" and soap.rows["CCTJobs"][1]["QtyReleased"] == 2


def test_readback_follows_the_last_write_and_covers_header_lines_and_jobs(live_writes):
    out, soap = go()
    last_write = max(i for i, c in enumerate(soap.calls) if c[0] in ("insert", "update"))
    after = [c for c in soap.calls[last_write + 1:] if c[0] == "load"]
    assert len(after) == 4  # CO header + lines + one read per job


def test_a_missing_or_wrong_row_at_readback_is_needs_reconciliation(live_writes):
    soap = FakeSoap(customers=CUST, corrupt={("CCT_Coitems", "Item"): "OTHER"})
    out, _ = go(soap=soap)
    assert out["state"] == "needs_reconciliation" and out["code"] == errors.READBACK_MISMATCH
    assert all(r["status"] == "unverified" for r in out["results"])
    assert any("item differs" in w for r in out["results"] for w in r["warnings"])


def test_a_line_that_does_not_reference_its_job_is_not_reported_saved(live_writes):
    soap = FakeSoap(customers=CUST, corrupt={("CCT_Coitems", idos.LINK_NUM_PROP): "9999"})
    out, _ = go(soap=soap)
    assert out["state"] == "needs_reconciliation" and out["code"] == errors.READBACK_MISMATCH
    assert any("reference its job" in w for r in out["results"] for w in r["warnings"])


def test_readback_failure_is_uncertain_not_success(live_writes):
    out, _ = go(soap=FakeSoap(customers=CUST, readback_fail=True))
    assert out["state"] == "needs_reconciliation" and out["code"] == errors.READBACK_MISMATCH
    assert all(r["status"] == "unverified" for r in out["results"])


def test_teardown_rejection_is_a_warning_and_the_job_still_saves(live_writes):
    req = save_req(2)
    req["lines"][0]["teardown"] = "2026-10-25"
    soap = FakeSoap(customers=CUST, scenarios={("update", "CCT_Coitems", 1): "reject"})
    out, soap = go(req=req, soap=soap)
    assert out["state"] == "succeeded"
    assert out["results"][0]["status"] == "saved" and "teardown date not set" in out["results"][0]["warnings"][0]
    assert soap.count("CCTJobs") == 2


def test_teardown_with_an_unknown_outcome_stops_the_run(live_writes):
    req = save_req(2)
    req["lines"][0]["teardown"] = "x"
    soap = FakeSoap(customers=CUST, scenarios={("update", "CCT_Coitems", 1): "timeout_after"})
    out, soap = go(req=req, soap=soap)
    assert out["state"] == "needs_reconciliation" and out["code"] == errors.UNCERTAIN_OUTCOME
    assert out["step"] == "teardown_1" and soap.count("CCTJobs") == 0
    assert [r["status"] for r in out["results"]] == ["unverified", "not_attempted"]


def test_no_teardown_means_no_update_call(live_writes):
    out, soap = go()
    assert ("update", "CCT_Coitems") not in soap.calls


def test_live_writes_off_fails_before_any_write():
    out, soap = go()
    assert out["state"] == "failed" and out["code"] == errors.LIVE_WRITES_DISABLED
    assert soap.writes() == [] and all(r["status"] == "not_attempted" for r in out["results"])


def test_header_rejected_with_nothing_written_is_a_definite_failure(live_writes):
    soap = FakeSoap(customers=CUST, scenarios={("insert", "SLCos", 1): "reject"})
    out, soap = go(soap=soap)
    assert out["state"] == "failed" and out["code"] == errors.ERP_REJECTED and out["step"] == "create_co"
    assert soap.count("CCT_Coitems") == 0


def test_time_budget_exhausted_before_the_first_write_fails_cleanly(live_writes):
    out, soap = go(remaining_ms=lambda: 100)
    assert out["state"] == "failed" and out["code"] == errors.TIME_BUDGET_EXHAUSTED and soap.writes() == []


# ---- US3: failure classification (research.md R7) -------------------------------------------
def test_rejected_line_after_saved_lines_is_partly_saved_when_its_rows_are_proven_absent(live_writes):
    soap = FakeSoap(customers=CUST, scenarios={("insert", "CCT_Coitems", 2): "reject"})
    out, soap = go(n=3, soap=soap)
    assert out["state"] == "partly_saved" and out["code"] == errors.ERP_REJECTED and out["step"] == "create_line_2"
    assert [r["status"] for r in out["results"]] == ["saved", "failed", "not_attempted"]
    assert "rejected" in out["results"][1]["error"] and soap.count("CCT_Coitems") == 1 and soap.count("CCTJobs") == 1
    assert "possibly_written" not in out


def test_rejected_first_line_leaves_a_header_so_it_needs_reconciliation(live_writes):
    soap = FakeSoap(customers=CUST, scenarios={("insert", "CCT_Coitems", 1): "reject"})
    out, _ = go(soap=soap)
    assert out["state"] == "needs_reconciliation" and out["code"] == errors.ERP_REJECTED
    assert {"kind": "co", "key": "100"} in out["possibly_written"]
    assert [r["status"] for r in out["results"]] == ["failed", "not_attempted"]


def test_rejected_job_leaves_a_line_without_a_job_so_it_needs_reconciliation(live_writes):
    soap = FakeSoap(customers=CUST, scenarios={("insert", "CCTJobs", 2): "reject"})
    out, _ = go(n=3, soap=soap)
    assert out["state"] == "needs_reconciliation" and out["step"] == "create_job_2"
    assert [r["status"] for r in out["results"]] == ["saved", "unverified", "not_attempted"]
    assert {"kind": "line", "key": "100/2"} in out["possibly_written"]


def test_rejection_whose_absence_cannot_be_proven_needs_reconciliation(live_writes):
    class NoReadsAfterLine2(FakeSoap):
        def load(self, ido, properties, filter_="", record_cap=0):
            if "AND CoLine = 2" in filter_:
                from syteline.errors import ErpUnreachable
                raise ErpUnreachable("read failed")
            return super().load(ido, properties, filter_, record_cap)

        read_back = load  # the fake aliases read_back to its own load

    soap = NoReadsAfterLine2(customers=CUST, scenarios={("insert", "CCT_Coitems", 2): "reject"})
    out, _ = go(n=3, soap=soap)
    assert out["state"] == "needs_reconciliation" and out["code"] == errors.ERP_REJECTED


@pytest.mark.parametrize("ido,nth", [("CCT_Coitems", 2), ("CCTJobs", 2)])
@pytest.mark.parametrize("how", ["timeout_after", "timeout_before"])
def test_unknown_outcome_is_never_classified_as_failed_or_retried(live_writes, ido, nth, how):
    soap = FakeSoap(customers=CUST, scenarios={("insert", ido, nth): how})
    out, soap = go(n=3, soap=soap)
    assert out["state"] == "needs_reconciliation" and out["code"] == errors.UNCERTAIN_OUTCOME
    assert soap.calls.count(("insert", "SLCos")) == 1  # nothing was repeated
    assert soap.count("CCT_Coitems") <= 2 and not any(r["status"] == "saved" for r in out["results"][1:])


def test_no_failure_injection_removes_or_repeats_a_write(live_writes):
    for scen in ({("insert", "CCT_Coitems", 2): "reject"}, {("insert", "CCTJobs", 1): "timeout_after"},
                 {("update", "CCT_Coitems", 1): "timeout_after"}):
        req = save_req(3)
        req["lines"][0]["teardown"] = "t"
        out, soap = go(n=3, req=req, soap=FakeSoap(customers=CUST, scenarios=scen))
        assert soap.calls.count(("insert", "SLCos")) == 1 and soap.count("SLCos") == 1  # header written once
        assert soap.calls.count(("update", "CCT_Coitems")) <= 1  # the teardown update is never repeated
        assert not hasattr(soap, "delete")  # and the client has no way to remove a row


# ---- customer defaults on the header (feature 005) ---------------------------------------------------------
from fakes import DB_CFG, FakeSqlServer  # noqa: E402

CO_ROWS = [{"co_num": 1, "cust_num": "C1", "cust_seq": 0, "terms_code": "N30", "ship_code": "UPS"},
           {"co_num": 2, "cust_num": "C1", "cust_seq": 0, "terms_code": "N45", "ship_code": "FDX"}]


def go_defaults(monkeypatch, server, n=2, **kw):
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "database")
    server.install(monkeypatch)
    soap = FakeSoap(customers=CUST)
    out = savewf.run_save(reservation(n), save_req(n), soap, dict(CFG, **DB_CFG), **kw)
    return out, soap


def test_the_header_carries_the_customers_terms_and_ship_via_from_its_latest_order(monkeypatch, live_writes):
    out, soap = go_defaults(monkeypatch, FakeSqlServer(co_rows=CO_ROWS))
    head = soap.rows["SLCos"][0]
    assert head[idos.CO_HEADER_PROPS["terms_code"]] == "N45" and head[idos.CO_HEADER_PROPS["ship_code"]] == "FDX"
    assert out["state"] == "succeeded" and out["warnings"] == []


def test_a_customer_without_orders_saves_without_codes_and_says_so(monkeypatch, live_writes):
    out, soap = go_defaults(monkeypatch, FakeSqlServer(co_rows=[]))
    head = soap.rows["SLCos"][0]
    assert idos.CO_HEADER_PROPS["terms_code"] not in head and idos.CO_HEADER_PROPS["ship_code"] not in head
    assert out["state"] == "succeeded" and out["warnings"] == ["customer defaults not available: no previous order "
                                                              "for this customer"]


def test_an_unreadable_default_never_blocks_the_save(monkeypatch, live_writes):
    out, soap = go_defaults(monkeypatch, FakeSqlServer(connect_error="connect_fail"))
    assert out["state"] == "succeeded" and soap.count("SLCos") == 1
    assert out["warnings"][0].startswith("customer defaults not available:")


def test_missing_database_settings_are_only_a_warning_for_the_defaults(monkeypatch, live_writes):
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "database")
    f = FakeSqlServer().install(monkeypatch)
    soap = FakeSoap(customers=CUST)
    out = savewf.run_save(reservation(), save_req(), soap, dict(CFG), )
    assert out["state"] == "succeeded" and "database settings missing" in out["warnings"][0] and f.connect_kwargs == []


def test_reading_the_defaults_does_not_change_the_order_of_erp_writes(monkeypatch, live_writes):
    out, soap = go_defaults(monkeypatch, FakeSqlServer(co_rows=CO_ROWS))
    assert kinds(soap) == [("insert", "SLCos"), ("insert", "CCT_Coitems"), ("insert", "CCTJobs"),
                           ("insert", "CCT_Coitems"), ("insert", "CCTJobs")]


def test_the_defaults_are_not_read_when_live_writes_are_off(monkeypatch):
    f = FakeSqlServer(co_rows=CO_ROWS).install(monkeypatch)
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "database")
    out = savewf.run_save(reservation(), save_req(), FakeSoap(customers=CUST), dict(CFG, **DB_CFG))
    assert out["state"] == "failed" and f.connect_kwargs == []


# ---- a job number the document supplied (FR-006e, research R24) --------------------------------------------
def supplied_reservation(n=2):
    r = reservation(n)
    r["job_numbers"][1] = {"line": 2, "job": "J-77", "source": "document"}
    return r


def test_a_supplied_job_number_is_saved_like_any_reserved_one_without_a_numbering_call(live_writes):
    soap = FakeSoap(customers=CUST)
    out = savewf.run_save(supplied_reservation(), save_req(2), soap, CFG)
    assert out["state"] == "succeeded" and [r["job"] for r in out["results"]] == ["5000", "J-77"]
    assert soap.rows["CCTJobs"][1]["Job"] == "J-77" and soap.rows["CCT_Coitems"][1][idos.LINE_LINK_PROPS["ref_num"]] == "J-77"
    assert not any(c[0] == "invoke" for c in soap.calls)


def test_the_erp_rejecting_a_supplied_job_number_needs_reconciliation_and_nothing_is_retried(live_writes):
    soap = FakeSoap(customers=CUST, scenarios={("insert", "CCTJobs", 2): "reject"})  # for example: the job exists
    out = savewf.run_save(supplied_reservation(), save_req(2), soap, CFG)
    assert out["state"] == "needs_reconciliation" and out["step"] == "create_job_2"
    assert {"kind": "line", "key": "100/2"} in out["possibly_written"] and {"kind": "co", "key": "100"} in out["possibly_written"]
    assert [c for c in soap.calls if c[:2] == ("insert", "CCTJobs")].__len__() == 2  # no retry


def test_the_received_date_is_written_only_when_its_property_is_configured(monkeypatch, live_writes):
    req = save_req(1, received_date="2026-10-19")
    monkeypatch.setenv("SYTELINE_CO_RECEIVED_DATE_PROP", "Datefld")
    _, soap = go(n=1, req=req)
    assert soap.rows["SLCos"][0]["Datefld"] == "2026-10-19"
    monkeypatch.delenv("SYTELINE_CO_RECEIVED_DATE_PROP")
    _, soap = go(n=1, req=req)
    assert "2026-10-19" not in soap.rows["SLCos"][0].values()


def test_a_numeric_customer_number_is_written_as_the_erp_stores_it(live_writes):
    """CustNum is a right-aligned, 7-character NumSortedString: "1234" is sent as "   1234" (the read-back compares stripped)."""
    assert idos.erp_cust_num("1234") == "   1234" and idos.erp_cust_num(" 1234567 ") == "1234567"
    assert idos.erp_cust_num("C1") == "C1" and idos.erp_cust_num("") == ""
    soap = FakeSoap(customers=CUST)
    res = reservation(1)
    res["customer_match"] = {"cust_num": "1234", "cust_seq": 0}
    out = savewf.run_save(res, save_req(1), soap, CFG)
    assert soap.rows["SLCos"][0]["CustNum"] == "   1234" and soap.rows["SLCos"][0]["CustSeq"] == 0
    assert out["state"] == "succeeded"
