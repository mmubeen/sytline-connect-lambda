import pytest

from fakes import FakeJobsTable
from syteline import reservations as rs
from syteline.jobs import RefConflict
from syteline.reservations import ReservationStore

REQ = {"request_ref": "r1", "document_id": "b/PO-1.pdf", "line_count": 2}


def mk(clock=None):
    return ReservationStore(FakeJobsTable(), clock=clock or (lambda: 1000))


def new(store, ref="r1", h="h", **kw):
    req = dict(REQ, request_ref=ref, **kw)
    return store.create_reservation(req, h, "fp", "u", "arn")


def test_create_is_atomic_and_starts_reserving():
    s = mk()
    r, dup = new(s)
    assert dup is False and r["state"] == rs.RESERVING and r["job_numbers"] == []
    assert s.get(r["reservation_id"])["document_id"] == "b/PO-1.pdf"
    assert "RESREF#r1" in s._t.items and "ttl" not in r


def test_same_ref_same_hash_returns_existing_and_different_hash_conflicts():
    s = mk()
    first, _ = new(s)
    again, dup = new(s)
    assert dup is True and again["reservation_id"] == first["reservation_id"]
    with pytest.raises(RefConflict):
        new(s, h="other")


def test_numbers_are_recorded_as_obtained_only_while_reserving():
    s = mk()
    r, _ = new(s)
    rid = r["reservation_id"]
    assert s.set_customer(rid, {"cust_num": "C1", "cust_seq": 0}) and s.set_co_num(rid, "100")
    assert s.add_job_number(rid, [{"line": 1, "job": "5000"}])
    assert s.mark_reserved(rid)
    got = s.get(rid)
    assert got["state"] == rs.RESERVED and got["co_num"] == "100" and got["job_numbers"] == [{"line": 1, "job": "5000"}]
    assert s.add_job_number(rid, []) is False  # no longer reserving: numbers cannot change


def test_accept_save_works_once():
    s = mk()
    rid = new(s)[0]["reservation_id"]
    assert s.accept_save(rid, {"document_id": "b/PO-1.pdf", "warehouse": "W"}) is False  # still reserving
    s.mark_reserved(rid)
    assert s.accept_save(rid, {"document_id": "b/PO-1.pdf", "warehouse": "W"}) is True
    assert s.accept_save(rid, {"document_id": "b/PO-1.pdf", "warehouse": "W"}) is False
    got = s.get(rid)
    assert got["state"] == rs.SAVE_ACCEPTED and got["save"]["document_id"] == "b/PO-1.pdf"
    assert got["save"]["warehouse"] == "W" and "requested_at" in got["save"]
    assert got["document_id"] == "b/PO-1.pdf"


def ready(s):
    rid = new(s)[0]["reservation_id"]
    s.mark_reserved(rid)
    s.accept_save(rid, {"document_id": "d"})
    return rid


def test_claim_only_from_save_accepted_and_counts_attempts():
    s = mk()
    rid = ready(s)
    assert s.claim(rid) is True and s.get(rid)["state"] == rs.SAVING and s.get(rid)["attempts"] == 1
    assert s.claim(rid) is False


def test_terminal_states_set_ttl_and_needs_reconciliation_is_final():
    s = mk()
    rid = ready(s)
    s.claim(rid)
    assert s.park(rid, "UNCERTAIN_OUTCOME", "m", "create_job_1", [{"kind": "co", "key": "100"}])
    got = s.get(rid)
    assert got["state"] == rs.NEEDS_RECONCILIATION and got["ttl"] > 1000
    assert got["erp_records"]["possibly_written"] == [{"kind": "co", "key": "100"}]
    for move in (lambda: s.finish_saved(rid, []), lambda: s.fail_save(rid, "c", "m", "s"),
                 lambda: s.finish_partly_saved(rid, "c", "m", "s", []), lambda: s.claim(rid),
                 lambda: s.mark_failed(rid, "c", "m", "s"), lambda: s.accept_save(rid, {"document_id": "d"})):
        assert move() is False
    assert s.get(rid)["state"] == rs.NEEDS_RECONCILIATION


def test_failed_reservation_cannot_be_saved():
    s = mk()
    rid = new(s)[0]["reservation_id"]
    assert s.mark_failed(rid, "ERP_REJECTED", "m", "number_co")
    assert s.mark_reserved(rid) is False and s.accept_save(rid, {"document_id": "d"}) is False
    assert s.get(rid)["failure"]["code"] == "ERP_REJECTED"


def test_progress_and_finish_saved():
    s = mk()
    rid = ready(s)
    s.claim(rid)
    assert s.record_progress(rid, [{"kind": "co", "key": "100"}], [{"line": 1}])
    assert s.finish_saved(rid, [{"line": 1, "status": "saved"}])
    assert s.get(rid)["state"] == rs.SAVED and s.get(rid)["results"][0]["status"] == "saved"


def test_sweeper_lists_only_the_matching_state_and_age():
    now = [1000]
    s = mk(clock=lambda: now[0])
    a = ready(s)  # save_accepted at 1000
    r2 = new(s, ref="r2")[0]["reservation_id"]
    s.mark_reserved(r2)  # reserved: never listed
    now[0] = 1200
    assert [i["reservation_id"] for i in s.list_save_accepted_older_than(120)] == [a]
    assert s.list_save_accepted_older_than(500) == []
    s.claim(a)
    now[0] = 1400
    assert [i["reservation_id"] for i in s.list_saving_older_than(150)] == [a]
    assert s.list_save_accepted_older_than(0) == []


# ---- numbers_report: what became of each reserved number ------------------------------------
def reserved_item(s, n=2, ref="r9"):
    rid = s.create_reservation({"request_ref": ref, "document_id": "d", "line_count": n}, "h", "f", "u", "a")[0]["reservation_id"]
    s.set_co_num(rid, "100")
    s.add_job_number(rid, [{"line": i, "job": str(5000 + i - 1)} for i in range(1, n + 1)])
    return rid


def usage(item, now=1000):
    return [j["usage"] for j in rs.numbers_report(item, now)["jobs"]]


def test_numbers_are_reserved_until_saved_and_then_used():
    s = mk()
    rid = reserved_item(s)
    s.mark_reserved(rid)
    assert usage(s.get(rid)) == ["reserved", "reserved"]
    s.accept_save(rid, {"document_id": "d"})
    s.claim(rid)
    assert usage(s.get(rid)) == ["reserved", "reserved"]
    s.finish_saved(rid, [{"line": 1, "status": "saved"}, {"line": 2, "status": "saved"}])
    assert usage(s.get(rid)) == ["used", "used"]
    assert rs.numbers_report(s.get(rid), 1000) == {"co_num": "100", "jobs": [
        {"line": 1, "job": "5000", "source": "allocated", "usage": "used"},
        {"line": 2, "job": "5001", "source": "allocated", "usage": "used"}]}


def test_a_failed_reservation_reports_every_number_consumed_unused():
    s = mk()
    rid = reserved_item(s)
    s.mark_failed(rid, "ERP_REJECTED", "m", "number_job_3")
    assert usage(s.get(rid)) == ["consumed_unused", "consumed_unused"]


def test_partly_saved_and_needs_reconciliation_report_per_line():
    s = mk()
    a = reserved_item(s, ref="a")
    s.mark_reserved(a); s.accept_save(a, {"document_id": "d"}); s.claim(a)
    s.finish_partly_saved(a, "ERP_REJECTED", "m", "create_line_2", [
        {"line": 1, "status": "saved"}, {"line": 2, "status": "failed"}])
    assert usage(s.get(a)) == ["used", "consumed_unused"]
    b = reserved_item(s, ref="b")
    s.mark_reserved(b); s.accept_save(b, {"document_id": "d"}); s.claim(b)
    s.park(b, "UNCERTAIN_OUTCOME", "m", "create_job_2", [], [{"line": 1, "status": "saved"},
                                                           {"line": 2, "status": "unverified"}])
    assert usage(s.get(b)) == ["used", "unverified"]


def test_a_reserving_item_that_never_finished_reads_as_failed_with_its_numbers_consumed():
    s = mk(clock=lambda: 1000)
    rid = reserved_item(s)
    item = s.get(rid)
    assert rs.effective_state(item, 1000 + 30) == rs.RESERVING and usage(item, 1030) == ["reserved", "reserved"]
    assert rs.effective_state(item, 1000 + rs.RESERVE_STALE_SECONDS + 1) == rs.FAILED
    assert usage(item, 1000 + rs.RESERVE_STALE_SECONDS + 1) == ["consumed_unused", "consumed_unused"]


def test_decimals_from_the_table_are_handled():
    from decimal import Decimal

    item = {"state": "reserved", "updated_at": Decimal(1000), "co_num": "100",
            "job_numbers": [{"line": Decimal(1), "job": "5000"}]}
    assert rs.numbers_report(item, 1000)["jobs"] == [{"line": 1, "job": "5000", "source": "allocated", "usage": "reserved"}]


def test_release_claim_returns_a_run_that_wrote_nothing_to_save_accepted():
    s = mk()
    rid = ready(s)
    assert s.release_claim(rid) is False  # not saving yet
    s.claim(rid)
    assert s.release_claim(rid) is True and s.get(rid)["state"] == rs.SAVE_ACCEPTED
    assert s.claim(rid) is True and s.get(rid)["attempts"] == 2


def test_no_po_details_are_ever_stored():
    s = mk()
    rid = ready(s)
    text = repr(s._t.items)
    for detail in ("ACME", "'PO-1'", "po_number", "lines", "quantity"):
        assert detail not in text


# ---- supplied job numbers (FR-006e, research R26) ---------------------------------------------
def with_supplied(s, ref="s1"):
    """Two lines: line 1 allocated (an older entry without `source`), line 2 supplied by the document."""
    rid = s.create_reservation({"request_ref": ref, "document_id": "d", "line_count": 2}, "h", "f", "u", "a")[0]["reservation_id"]
    s.set_co_num(rid, "100")
    s.add_job_number(rid, [{"line": 1, "job": "5000"}, {"line": 2, "job": "J-77", "source": "document"}])
    return rid


def sources(item):
    return [j["source"] for j in rs.numbers_report(item, 1000)["jobs"]]


def test_the_report_shows_the_source_and_reads_an_entry_without_one_as_allocated():
    s = mk()
    rid = with_supplied(s)
    s.mark_reserved(rid)
    assert sources(s.get(rid)) == ["allocated", "document"] and usage(s.get(rid)) == ["reserved", "reserved"]
    s.accept_save(rid, {"document_id": "d"}); s.claim(rid)
    s.finish_saved(rid, [{"line": 1, "status": "saved"}, {"line": 2, "status": "saved"}])
    assert usage(s.get(rid)) == ["used", "used"]


def test_a_supplied_number_that_was_not_saved_is_not_reported_as_consumed():
    s = mk()
    failed = with_supplied(s, "f1")
    s.mark_failed(failed, "ERP_REJECTED", "m", "number_job_3")
    assert usage(s.get(failed)) == ["consumed_unused", "supplied_not_saved"]
    partly = with_supplied(s, "p1")
    s.mark_reserved(partly); s.accept_save(partly, {"document_id": "d"}); s.claim(partly)
    s.finish_partly_saved(partly, "ERP_REJECTED", "m", "create_line_1", [
        {"line": 1, "status": "failed"}, {"line": 2, "status": "not_attempted"}])
    assert usage(s.get(partly)) == ["consumed_unused", "supplied_not_saved"]
    recon = with_supplied(s, "r1")
    s.mark_reserved(recon); s.accept_save(recon, {"document_id": "d"}); s.claim(recon)
    s.park(recon, "UNCERTAIN_OUTCOME", "m", "create_job_2", [], [{"line": 1, "status": "saved"},
                                                               {"line": 2, "status": "unverified"}])
    assert usage(s.get(recon)) == ["used", "unverified"]
