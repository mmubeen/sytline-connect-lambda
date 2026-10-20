"""add_note / get_notes on CO and job records, over SQL (see research.md R10).

Reads follow the joins in sql/NotesExampleQueries.txt. add_note calls CreateSpecificNoteSp, which
creates the note and attaches it to the row; it is a write and is gated by SYTELINE_LIVE_WRITES.

ASSUMPTION: the SQL transport (the instruction sheet does not mention notes).
"""

from . import errors
from .errors import (ErpUncertain, ErpUnreachable, LiveWritesDisabled, error, failure)

# record_type -> (table, key column, ObjectName used by the note procedures)
RECORDS = {"co": ("co", "co_num", "co"), "job": ("job", "job", "job")}


def validate_note_request(event, need_text):
    errs = []
    if event.get("record_type") not in RECORDS:
        errs.append(error(errors.VALIDATION, "record_type must be 'co' or 'job'", field="record_type"))
    if not isinstance(event.get("record_key"), str) or not event["record_key"].strip():
        errs.append(error(errors.VALIDATION, "record_key is required", field="record_key"))
    if need_text and (not isinstance(event.get("text"), str) or not event["text"].strip()):
        errs.append(error(errors.VALIDATION, "text is required", field="text"))
    since = event.get("since")
    if since is not None:
        from .validation import _is_date

        if not _is_date(since):
            errs.append(error(errors.VALIDATION, "since must be YYYY-MM-DD", field="since"))
    return errs


def _row_pointer(sql, record_type, record_key):
    table, column, _ = RECORDS[record_type]
    rows = sql.query(f"SELECT RowPointer FROM {table} WHERE {column} = %s", (record_key,))
    return rows[0]["RowPointer"] if rows else None


def add_note(sql, record_type, record_key, text, internal=True):
    rp = _row_pointer(sql, record_type, record_key)
    if rp is None:
        return failure("add_note", error(errors.NOT_FOUND, f"{record_type} {record_key} not found"))
    try:
        sql.execute_write(
            "DECLARE @Infobar Infobar; "
            "EXEC dbo.CreateSpecificNoteSp %s, %s, %s, %s, %s, @Infobar OUTPUT; SELECT @Infobar AS Infobar",
            (RECORDS[record_type][2], rp, text[:60], text, 1 if internal else 0))
    except LiveWritesDisabled:
        return failure("add_note", error(errors.LIVE_WRITES_DISABLED, "live ERP writes are disabled"))
    except ErpUncertain as exc:  # the note may exist; do not retry blindly
        return failure("add_note", error(errors.UNCERTAIN_OUTCOME, str(exc)))
    except ErpUnreachable as exc:
        return failure("add_note", error(errors.ERP_UNREACHABLE, str(exc)))
    return {"ok": True, "action": "add_note"}


_NOTES_SQL = (
    "SELECT sn.SpecificNoteToken, sn.NoteContent, sn.NoteDesc, sn.CreateDate, sn.CreatedBy, "
    "ObjectNotes.NoteFlag AS NhNoteFlag, ObjectNotes.RefRowPointer "
    "FROM SpecificNotes sn "
    "INNER JOIN ObjectNotes ON sn.SpecificNoteToken = ObjectNotes.SpecificNoteToken "
    "WHERE ObjectNotes.RefRowPointer = %s")


def get_notes(sql, record_type, record_key, since=None):
    rp = _row_pointer(sql, record_type, record_key)
    if rp is None:
        return failure("get_notes", error(errors.NOT_FOUND, f"{record_type} {record_key} not found"))
    query, params = _NOTES_SQL, [rp]
    if since:
        query += " AND sn.CreateDate >= %s"
        params.append(since)
    rows = sql.query(query + " ORDER BY sn.SpecificNoteToken", tuple(params))
    return {"ok": True, "action": "get_notes", "notes": [{
        "token": int(float(r["SpecificNoteToken"])) if r.get("SpecificNoteToken") is not None else None,
        "text": r.get("NoteContent") or r.get("DerContent") or "",
        "internal": str(r.get("NhNoteFlag", "")) in ("1", "True", "true"),
        "created_at": str(r.get("CreateDate", "")), "created_by": r.get("CreatedBy", ""),
    } for r in rows]}
