import pytest

from fakes import FakeSoap
from syteline import errors, idos, rowbuild, savewf

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
    assert kinds(soap) == [("insert", "SLCos"), ("insert", "CCT_Coitems"), ("insert", "UserDefinedFields"),
                           ("insert", "CCTJobs"), ("insert", "CCT_Coitems"), ("insert", "CCTJobs")]
    assert not any(c == ("invoke", "NextWOSp") or c == ("invoke", "NextCoSp") for c in soap.calls)  # FR-005
    assert out["results"] == [
        {"line": 1, "job": "5000", "item": "I0", "status": "saved", "warnings": [], "co_line": 1},
        {"line": 2, "job": "5001", "item": "I1", "status": "saved", "warnings": [], "co_line": 2}]
    head = soap.rows["SLCos"][0]
    assert head["CoNum"] == "       100" and head["CustNum"] == "     C1" and head["CustSeq"] == "   0"  # padded to the ERP's widths
    assert head["CustPo"] == "PO-1".rjust(22) and head["OrderDate"] == "2026-10-19T00:00:00-04:00"  # the received date, Eastern
    assert {k: head[k] for k in ("Stat", "Whse", "TaxCode1", "FrtTaxCode1", "MscTaxCode1", "OrigSite")} == {
        "Stat": "P", "Whse": "MAIN", "TaxCode1": "R", "FrtTaxCode1": "N", "MscTaxCode1": "N", "OrigSite": "CCT"}
    assert "DateReceived" not in head
    line1 = soap.rows["CCT_Coitems"][0]  # lines are written to CCT_Coitems with lowercase names
    assert line1["co_num"] == "       100" and line1["co_line"] == "   1" and line1["item"] == "I0".rjust(30)
    assert line1["ref_num"] == "      5000" and line1["ref_type"] == "J" and line1["ref_line_suf"] == "0" and line1["u_m"] == "EA"
    assert (line1["qty_ordered"], line1["qty_ordered_conv"], line1["stat"], line1["ship_site"], line1["whse"]) == ("1", "1", "P", "CCT", "MAIN")
    assert "Teardown" not in line1 and "teardown" not in line1
    (udf,) = soap.rows["UserDefinedFields"]  # the teardown date is a UserDefinedFields row for the line's RowPointer
    assert udf["TableName"] == "coitem" and udf["UDFDT1"] == "2026-10-25T00:00:00-04:00"
    assert udf["RowId"] == soap.rows["SLCoitems"][0]["RowPointer"]
    job1 = soap.rows["CCTJobs"][0]
    link = idos.JOB_LINK_PROPS
    assert job1["Job"] == "      5000" and job1[link["co_num"]] == "       100" and job1[link["co_line"]] == 1
    assert job1["RootJob"] == "      5000" and "CoNum" not in job1 and "CoLine" not in job1  # the link back is OrdNum / OrdLine
    assert (job1["Type"], job1["Stat"], job1["OrdType"], job1["Suffix"], job1["RootSuf"], job1["OrdRelease"]) == ("J", "F", "O", 0, 0, 0)
    assert (job1["uf_eng_sn"], job1["uf_sn"], job1["uf_app"]) == ("E1", "S1", "CDBD")  # Box -> CDBD, Crate -> YES
    assert job1["JobDate"] == "2026-10-19T00:00:00-04:00" == job1["JschStartDate"] == job1["JschEndDate"] and job1["Whse"] == "MAIN"
    assert soap.rows["CCTJobs"][1]["Job"] == "      5001" and soap.rows["CCTJobs"][1]["QtyReleased"] == "2"


def test_readback_follows_the_last_write_and_covers_header_lines_and_jobs(live_writes):
    out, soap = go()
    last_write = max(i for i, c in enumerate(soap.calls) if c[0] in ("insert", "update"))
    after = [c for c in soap.calls[last_write + 1:] if c[0] == "load"]
    assert len(after) == 4  # CO header + lines + one read per job


def test_a_missing_or_wrong_row_at_readback_is_needs_reconciliation(live_writes):
    soap = FakeSoap(customers=CUST, corrupt={("SLCoitems", "Item"): "OTHER"})
    out, _ = go(soap=soap)
    assert out["state"] == "needs_reconciliation" and out["code"] == errors.READBACK_MISMATCH
    assert all(r["status"] == "unverified" for r in out["results"])
    assert any("item differs" in w for r in out["results"] for w in r["warnings"])


def test_a_line_that_does_not_reference_its_job_is_not_reported_saved(live_writes):
    soap = FakeSoap(customers=CUST, corrupt={("SLCoitems", idos.LINK_NUM_PROP): "9999"})
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
    soap = FakeSoap(customers=CUST, scenarios={("insert", "UserDefinedFields", 1): "reject"})
    out, soap = go(req=req, soap=soap)
    assert out["state"] == "succeeded"
    assert out["results"][0]["status"] == "saved" and "teardown date not set" in out["results"][0]["warnings"][0]
    assert soap.count("CCTJobs") == 2


def test_teardown_with_an_unknown_outcome_stops_the_run(live_writes):
    req = save_req(2)
    req["lines"][0]["teardown"] = "x"
    soap = FakeSoap(customers=CUST, scenarios={("insert", "UserDefinedFields", 1): "timeout_after"})
    out, soap = go(req=req, soap=soap)
    assert out["state"] == "needs_reconciliation" and out["code"] == errors.UNCERTAIN_OUTCOME
    assert out["step"] == "teardown_1" and soap.count("CCTJobs") == 0
    assert [r["status"] for r in out["results"]] == ["unverified", "not_attempted"]


def test_no_teardown_means_no_teardown_row(live_writes):
    out, soap = go()
    assert ("insert", "UserDefinedFields") not in soap.calls and "UserDefinedFields" not in soap.rows


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
            if "AND CoLine = '   2'" in filter_:
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
                 {("insert", "UserDefinedFields", 1): "timeout_after"}):
        req = save_req(3)
        req["lines"][0]["teardown"] = "t"
        out, soap = go(n=3, req=req, soap=FakeSoap(customers=CUST, scenarios=scen))
        assert soap.calls.count(("insert", "SLCos")) == 1 and soap.count("SLCos") == 1  # header written once
        assert soap.calls.count(("insert", "UserDefinedFields")) <= 1  # the teardown row is never repeated
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
    assert soap.rows["CCTJobs"][1]["Job"].strip() == "J-77" and soap.rows["CCT_Coitems"][1]["ref_num"].strip() == "J-77"
    assert not any(c[0] == "invoke" for c in soap.calls)


def test_the_erp_rejecting_a_supplied_job_number_needs_reconciliation_and_nothing_is_retried(live_writes):
    soap = FakeSoap(customers=CUST, scenarios={("insert", "CCTJobs", 2): "reject"})  # for example: the job exists
    out = savewf.run_save(supplied_reservation(), save_req(2), soap, CFG)
    assert out["state"] == "needs_reconciliation" and out["step"] == "create_job_2"
    assert {"kind": "line", "key": "100/2"} in out["possibly_written"] and {"kind": "co", "key": "100"} in out["possibly_written"]
    assert [c for c in soap.calls if c[:2] == ("insert", "CCTJobs")].__len__() == 2  # no retry


def test_a_numeric_customer_number_is_padded_to_seven_and_the_readback_compares_trimmed(live_writes):
    assert rowbuild.rjust("1234", 7) == "   1234" and rowbuild.rjust(" 1234567 ", 7) == "1234567" and rowbuild.rjust(None, 3) == "   "
    soap = FakeSoap(customers=CUST)
    res = reservation(1)
    res["customer_match"] = {"cust_num": "1234", "cust_seq": 0}
    out = savewf.run_save(res, save_req(1), soap, CFG)
    assert soap.rows["SLCos"][0]["CustNum"] == "   1234" and soap.rows["SLCos"][0]["CustSeq"] == "   0"
    assert out["state"] == "succeeded"


def test_dates_are_iso_datetimes_with_the_eastern_offset_and_other_text_is_left_alone():
    assert rowbuild.erp_datetime("2026-10-19") == "2026-10-19T00:00:00-04:00"   # daylight time
    assert rowbuild.erp_datetime("01/15/2026") == "2026-01-15T00:00:00-05:00"   # standard time
    assert rowbuild.erp_datetime("soon") == "soon"
    assert rowbuild.number(3.0) == "3" and rowbuild.number(2.5) == "2.5" and rowbuild.number("x") == "x"
    assert [rowbuild.packing_code(v) for v in ("Box", "CRATE", "pallet")] == ["CDBD", "YES", "pallet"]


def test_without_a_found_default_the_header_has_no_terms_or_ship_code_and_the_defaults_win(live_writes):
    head = rowbuild.co_header("100", {"cust_num": "1", "cust_seq": 0}, {"po_number": "P", "order_date": "2026-10-20"}, None)
    assert "TermsCode" not in head and head["OrderDate"] == "2026-10-20T00:00:00-04:00" and head["Stat"] == "P"
    with_codes = rowbuild.co_header("100", {"cust_num": "1", "cust_seq": 0}, {"po_number": "P"}, {"terms_code": "N30", "ship_code": "FOB"})
    assert (with_codes["TermsCode"], with_codes["ShipCode"]) == ("N30", "FOB")


def test_a_line_without_a_due_date_uses_the_received_date_for_the_job_schedule(live_writes):
    req = {"po_number": "P", "received_date": "2026-10-19"}
    row = rowbuild.job_row("100", 1, {"item": "I", "quantity": 1, "uom": "EA", "due_date": "2026-11-01"}, "5000", req)
    assert row["JobDate"] == "2026-10-19T00:00:00-04:00" and row["JschStartDate"] == row["JschEndDate"] == "2026-11-01T00:00:00-04:00"


# ---- development mode (feature 009): the pre-created order is overwritten ----------------------------------------------
TARGET = "129999"


def dev_reservation(n=1):
    r = reservation(n)
    r.update(co_num=TARGET, dev_target=TARGET)
    return r


def precreate(soap, n=1, cust="C1"):
    """A target order as the ERP owner would have created it: a header and `n` lines, one job."""
    soap.add_record(idos.CO_IDO, CoNum=TARGET.rjust(10), CustNum=cust.rjust(7), CustPo="OLD".rjust(22))
    for i in range(1, n + 1):
        row = {"co_num": TARGET.rjust(10), "co_line": str(i).rjust(4), "co_release": "0", "item": "OLD", "qty_ordered": "9"}
        soap.add_record(idos.LINE_WRITE_IDO, **row)
        soap.add_record(idos.LINE_IDO, CoNum=row["co_num"], CoLine=row["co_line"], CoRelease="0", Item="OLD", QtyOrdered="9")
    return soap


def dev_go(n=1, soap=None, req=None, lines_before=1):
    soap = soap or FakeSoap(customers=CUST)
    if not soap.rows.get(idos.CO_IDO):
        precreate(soap, lines_before)
    out = savewf.run_save(dev_reservation(n), req or save_req(n), soap, CFG)
    return out, soap


@pytest.fixture
def devmode(live_writes, monkeypatch):
    monkeypatch.setenv("DEV_TARGET_CO", TARGET)


def test_the_header_is_updated_not_inserted_and_no_other_order_is_touched(devmode):
    out, soap = dev_go(lines_before=0)
    assert out["state"] == "succeeded" and out["co_action"] == "updated" and out["development"] == {"target": TARGET}
    assert ("insert", idos.CO_IDO) not in kinds(soap) and ("update", idos.CO_IDO) in kinds(soap)
    assert len(soap.rows[idos.CO_IDO]) == 1 and soap.rows[idos.CO_IDO][0]["CustPo"] == "PO-1".rjust(22)
    assert not any(c == ("invoke", "NextWOSp") or c == ("invoke", "NextCoSp") for c in soap.calls)


def test_an_existing_line_is_updated_and_a_missing_line_is_added(devmode):
    out, soap = dev_go(n=2, lines_before=1)
    assert out["state"] == "succeeded"
    assert [r["action"] for r in out["results"]] == ["updated", "inserted"]
    assert ("update", idos.LINE_WRITE_IDO) in kinds(soap) and kinds(soap).count(("insert", idos.LINE_WRITE_IDO)) == 1
    qty = {r["CoLine"].strip(): r["QtyOrdered"] for r in soap.rows[idos.LINE_IDO]}
    assert qty == {"1": "1", "2": "2"}  # line 1 now carries the document's quantity


def test_surplus_lines_on_the_target_are_left_and_reported(devmode):
    out, soap = dev_go(n=1, lines_before=3)
    assert out["state"] == "succeeded" and out["surplus_lines"] == [2, 3]
    assert any("left as they are" in w for w in out["warnings"])
    assert len(soap.rows[idos.LINE_WRITE_IDO]) == 3  # nothing deleted


def test_the_second_run_overwrites_the_first(devmode, monkeypatch):
    monkeypatch.setenv("JOB_NUMBER", "5000")
    out1, soap = dev_go(lines_before=0)
    assert out1["state"] == "succeeded" and kinds(soap).count(("insert", idos.JOB_IDO)) == 1
    req = save_req(1)
    req["lines"][0].update(item="NEW", quantity=7)
    out2, _ = dev_go(soap=soap, req=req)
    assert out2["state"] == "succeeded" and kinds(soap).count(("insert", idos.JOB_IDO)) == 1  # the job was updated the second time
    assert ("update", idos.JOB_IDO) in kinds(soap) and len(soap.rows[idos.JOB_IDO]) == 1
    assert len(soap.rows[idos.CO_IDO]) == 1 and soap.rows[idos.LINE_IDO][0]["QtyOrdered"] == "7"


def test_a_missing_target_writes_nothing(devmode):
    out, soap = dev_go(soap=FakeSoap(customers=CUST))
    soap.rows.pop(idos.CO_IDO, None)
    out = savewf.run_save(dev_reservation(), save_req(1), soap, CFG)
    assert out["state"] == "failed" and out["code"] == errors.DEV_TARGET_MISSING and out["step"] == "check_target"


def test_a_target_of_another_customer_writes_nothing(devmode):
    soap = FakeSoap(customers=CUST)
    precreate(soap, 1, cust="OTHER")
    out, _ = dev_go(soap=soap)
    assert out["state"] == "failed" and out["code"] == errors.DEV_TARGET_CUSTOMER_MISMATCH and soap.writes() == []


def test_a_rejected_first_update_changed_nothing(devmode):
    soap = FakeSoap(customers=CUST, scenarios={("update", idos.CO_IDO, 1): "reject"})
    out, _ = dev_go(soap=soap)
    assert out["state"] == "failed" and out["code"] == errors.ERP_REJECTED and out["step"] == "update_co"


def test_a_rejection_after_the_header_was_updated_needs_reconciliation(devmode):
    soap = FakeSoap(customers=CUST, scenarios={("update", idos.LINE_WRITE_IDO, 1): "reject"})
    out, _ = dev_go(soap=soap, lines_before=1)
    assert out["state"] == "needs_reconciliation" and out["step"] == "update_line_1"
    assert ("delete", idos.CO_IDO) not in soap.calls


def test_a_target_changed_since_the_reservation_is_refused(live_writes, monkeypatch):
    monkeypatch.setenv("DEV_TARGET_CO", "555")
    soap = precreate(FakeSoap(customers=CUST))
    out = savewf.run_save(dev_reservation(), save_req(1), soap, CFG)
    assert out["state"] == "failed" and out["code"] == errors.DEV_TARGET_INVALID and soap.writes() == []


def test_a_reservation_made_outside_development_mode_is_inserted_as_usual(live_writes):
    out, soap = go(1)
    assert out["state"] == "succeeded" and "development" not in out and ("insert", idos.CO_IDO) in kinds(soap)
