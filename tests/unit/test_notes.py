from fakes import FakeSoap
from syteline import idos, notes
from syteline.errors import ErpUncertain


def soap(**kw):
    s = FakeSoap(**kw)
    s.add_record("SLCos", CoNum="100")
    s.add_record("CCTJobs", Job="5000")
    return s


def test_add_note_loads_row_pointer_then_invokes_method(live_writes):
    s = soap()
    out = notes.add_note(s, "co", "100", "a" * 100, internal=True)
    assert out == {"ok": True, "action": "add_note"}
    assert [c for c in s.calls if c[0] in ("load", "invoke")] == [("load", "SLCos"), ("invoke", "CreateSpecificNoteSp")]
    assert s.rows["SpecificNotes"][0]["NoteContent"] == "a" * 100 and s.rows["SpecificNotes"][0]["NoteDesc"] == "a" * 60
    assert s.rows["ObjectNotes"][0]["RefRowPointer"] == s.rows["SLCos"][0]["RowPointer"]


def test_internal_flag_is_1_external_0(live_writes):
    s = soap()
    notes.add_note(s, "job", "5000", "a", internal=True)
    notes.add_note(s, "job", "5000", "b", internal=False)
    assert [r["NhNoteFlag"] for r in s.rows["ObjectNotes"]] == ["1", "0"]


def test_job_notes_use_the_job_collection_and_key(live_writes):
    s = soap()
    notes.add_note(s, "job", "5000", "x")
    assert ("load", "CCTJobs") in s.calls and ("load", "SLCos") not in s.calls


def test_record_key_is_escaped_in_the_filter():
    s = soap()
    out = notes.get_notes(s, "job", "5000'; DROP TABLE job;--")
    assert out["ok"] is False and out["errors"][0]["code"] == "NOT_FOUND"
    assert idos.quote("5000'; DROP TABLE job;--") == "'5000''; DROP TABLE job;--'"


def test_get_notes_reads_object_notes_then_specific_notes(live_writes):
    s = soap()
    notes.add_note(s, "co", "100", "hello", internal=False)
    s.calls.clear()
    out = notes.get_notes(s, "co", "100")
    assert [c for c in s.calls] == [("load", "SLCos"), ("load", "ObjectNotes"), ("load", "SpecificNotes")]
    assert out["notes"][0]["text"] == "hello" and out["notes"][0]["internal"] is False


def test_missing_record_writes_nothing(live_writes):
    s = soap()
    out = notes.add_note(s, "job", "nope", "t")
    assert out["errors"][0]["code"] == "NOT_FOUND" and s.writes() == []


def test_live_writes_off_blocks_add_note_but_not_get_notes():
    s = soap()
    assert notes.add_note(s, "co", "100", "t")["errors"][0]["code"] == "LIVE_WRITES_DISABLED"
    assert s.writes() == [] and notes.get_notes(s, "co", "100")["ok"] is True


def test_uncertain_invoke_is_reported_and_not_retried(live_writes):
    s = soap(scenarios={("invoke", "CreateSpecificNoteSp", 1): "timeout_after"})
    out = notes.add_note(s, "co", "100", "t")
    assert out["errors"][0]["code"] == "UNCERTAIN_OUTCOME"
    assert s.calls.count(("invoke", "CreateSpecificNoteSp")) == 1


# ---- the document's notes on the order (feature 011) ----------------------------------------------------------------------
import pytest

from syteline.errors import ErpRejected

SECTIONS = [{"title": "Customer Requirements", "text": "Verify work."}, {"title": "Billing Information", "text": "E-mail invoices."}]
LINK = "SLObjectNotes"


def order(**kw):
    s = FakeSoap(**kw)
    s.add_record("SLCos", CoNum="       100")
    return s


def test_each_section_becomes_a_note_tied_to_the_order(live_writes):
    s = order()
    out = notes.attach_order_notes(s, "100", SECTIONS)
    assert out == {"attempted": True, "attached": ["Customer Requirements", "Billing Information"], "not_attached": [],
                   "skipped_existing": [], "replaced": []}
    assert [(r["NoteDesc"], r["NoteContent"]) for r in s.rows["SpecificNotes"]] == [("Customer Requirements", "Verify work."),
                                                                                   ("Billing Information", "E-mail invoices.")]
    pointer = s.rows["SLCos"][0]["RowPointer"]
    links = s.rows[LINK]
    assert [r["SpecificNoteToken"] for r in links] == [r["SpecificNoteToken"] for r in s.rows["SpecificNotes"]]
    assert all(r["NoteHeaderToken"] == 31 and r["RefRowPointer"] == pointer and r["NoteType"] == 0 for r in links)


def test_blank_sections_and_no_sections_write_nothing(live_writes):
    s = order()
    assert notes.attach_order_notes(s, "100", [{"title": "A", "text": "  "}])["attempted"] is False
    assert notes.attach_order_notes(s, "100", None)["attached"] == [] and s.writes() == []


def test_a_note_without_a_token_in_the_answer_is_found_by_its_text(live_writes):
    class NoToken(FakeSoap):
        filters = []

        def load(self, ido, properties, filter_="", record_cap=0):
            self.filters.append(filter_)
            return super().load(ido, properties, filter_, record_cap)

        def insert(self, ido, values):
            row = super().insert(ido, values)
            if ido == "SpecificNotes":
                row.pop("SpecificNoteToken", None)
                self.rows[ido][-1].update(SpecificNoteToken="77", CreateDate="9999-01-01")  # stored, but not in the answer
                return {k: v for k, v in row.items() if k != "SpecificNoteToken"}
            return row

    s = NoToken()
    s.add_record("SLCos", CoNum="       100")
    out = notes.attach_order_notes(s, "100", SECTIONS[:1])
    assert out["attached"] == ["Customer Requirements"] and s.rows[LINK][0]["SpecificNoteToken"] == "77"
    assert not any("NoteContent =" in f for f in s.filters)  # ntext: the ERP refuses "=" on it


def test_live_writes_off_attempts_nothing(monkeypatch):
    monkeypatch.delenv("SYTELINE_LIVE_WRITES", raising=False)
    s = order()
    out = notes.attach_order_notes(s, "100", SECTIONS)
    assert out["attempted"] is False and len(out["not_attached"]) == 2 and s.writes() == []


def test_an_order_without_a_row_pointer_gets_no_notes(live_writes):
    out = notes.attach_order_notes(FakeSoap(), "100", SECTIONS)
    assert out["attached"] == [] and "RowPointer" in out["not_attached"][0]["reason"]


# -- failures never raise, never retry --------------------------------------------------------------------------------------
def test_a_refused_note_is_reported_and_the_next_one_still_runs(live_writes):
    s = order(scenarios={("insert", "SpecificNotes", 1): "reject"})
    out = notes.attach_order_notes(s, "100", SECTIONS)
    assert out["attached"] == ["Billing Information"]
    assert out["not_attached"] == [{"title": "Customer Requirements", "uncertain": False, "reason": "ERP refused: SpecificNotes row rejected by ERP"}]


def test_an_unknown_outcome_stops_the_rest_and_is_never_retried(live_writes):
    s = order(scenarios={("insert", "SpecificNotes", 1): "timeout_before"})
    out = notes.attach_order_notes(s, "100", SECTIONS)
    assert out["attached"] == [] and [n["title"] for n in out["not_attached"]] == ["Customer Requirements", "Billing Information"]
    assert out["not_attached"][0]["uncertain"] is True and out["not_attached"][1]["uncertain"] is False
    assert s.writes().count(("insert", "SpecificNotes")) == 1


def test_an_unreachable_erp_stops_the_rest(live_writes):
    s = order(scenarios={("insert", "SpecificNotes", 2): "unreachable"})
    out = notes.attach_order_notes(s, "100", SECTIONS)
    assert out["attached"] == ["Customer Requirements"] and out["not_attached"][0]["title"] == "Billing Information"


def test_a_link_refusal_is_reported_for_that_note(live_writes):
    s = order(scenarios={("insert", LINK, 1): "reject"})
    out = notes.attach_order_notes(s, "100", SECTIONS)
    assert out["attached"] == ["Billing Information"] and out["not_attached"][0]["title"] == "Customer Requirements"


def test_too_little_time_stops_before_the_next_note(live_writes):
    s = order()
    out = notes.attach_order_notes(s, "100", SECTIONS, time_left=lambda: 10)
    assert out["attached"] == [] and "time" in out["not_attached"][0]["reason"] and s.writes() == []


def test_the_note_text_is_never_in_the_report(live_writes):
    s = order(scenarios={("insert", "SpecificNotes", 1): "reject"})
    assert "Verify work." not in str(notes.attach_order_notes(s, "100", SECTIONS))


# -- duplicates --------------------------------------------------------------------------------------------------------------
def test_a_title_the_order_already_has_is_skipped(live_writes):
    s = order()
    notes.attach_order_notes(s, "100", SECTIONS)
    again = notes.attach_order_notes(s, "100", SECTIONS)
    assert again["skipped_existing"] == ["Customer Requirements", "Billing Information"] and again["attached"] == []
    assert len(s.rows["SpecificNotes"]) == 2 and len(s.rows[LINK]) == 2


def test_replace_updates_the_text_and_inserts_a_missing_title_without_deleting(live_writes):
    s = order()
    notes.attach_order_notes(s, "100", SECTIONS[:1])
    out = notes.attach_order_notes(s, "100", [{"title": "Customer Requirements", "text": "New text."}, SECTIONS[1]], replace=True)
    assert out["replaced"] == ["Customer Requirements"] and out["attached"] == ["Billing Information"]
    assert [r["NoteContent"] for r in s.rows["SpecificNotes"]] == ["New text.", "E-mail invoices."] and len(s.rows[LINK]) == 2
    assert not [c for c in s.calls if c[0] == "delete"]
