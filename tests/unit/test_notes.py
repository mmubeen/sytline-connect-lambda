from fakes import FakeSql
from syteline import notes


def sql():
    return FakeSql(records={("co", "100"): "rp-co", ("job", "5000"): "rp-job"})


def test_internal_flag_is_1_external_0_and_proc_params(live_writes):
    s = sql()
    notes.add_note(s, "co", "100", "a" * 100, internal=True)
    notes.add_note(s, "co", "100", "b", internal=False)
    (_, p1), (_, p2) = s.writes
    assert p1 == ("co", "rp-co", "a" * 60, "a" * 100, 1) and p2[4] == 0
    assert "CreateSpecificNoteSp" in s.writes[0][0]


def test_lookup_is_parameterized():
    s = sql()
    notes.get_notes(s, "job", "5000'; DROP TABLE job;--")
    text, params = s.queries[0]
    assert "DROP" not in text and params == ("5000'; DROP TABLE job;--",)


def test_get_notes_uses_documented_joins():
    s = sql()
    notes.get_notes(s, "co", "100")
    text, params = s.queries[-1]
    assert "SpecificNotes" in text and "ObjectNotes" in text
    assert "sn.SpecificNoteToken = ObjectNotes.SpecificNoteToken" in text
    assert "ObjectNotes.RefRowPointer = %s" in text and params == ("rp-co",)


def test_missing_record_writes_nothing(live_writes):
    s = sql()
    out = notes.add_note(s, "job", "nope", "t")
    assert out["ok"] is False and out["errors"][0]["code"] == "NOT_FOUND" and s.writes == []


def test_live_writes_off_blocks_add_note_but_not_get_notes():
    s = sql()
    out = notes.add_note(s, "co", "100", "t")
    assert out["errors"][0]["code"] == "LIVE_WRITES_DISABLED" and s.writes == []
    assert notes.get_notes(s, "co", "100")["ok"] is True


def test_uncertain_write_is_reported_not_retried(live_writes):
    from syteline.errors import ErpUncertain

    class Boom(FakeSql):
        def execute_write(self, sql, params=()):
            self.writes.append(1)
            raise ErpUncertain("timeout")

    s = Boom(records={("co", "100"): "rp"})
    out = notes.add_note(s, "co", "100", "t")
    assert out["errors"][0]["code"] == "UNCERTAIN_OUTCOME" and len(s.writes) == 1
