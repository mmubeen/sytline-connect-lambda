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
