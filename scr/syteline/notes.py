"""add_note / get_notes on CO and job records, through the web service only (FR-018; research R10).

add_note: load the record's RowPointer, then call CreateSpecificNoteSp as a web-service method (it
creates AND attaches the note; a write, gated by SYTELINE_LIVE_WRITES).
get_notes: read ObjectNotes (RefRowPointer = the record's RowPointer) and SpecificNotes (joined on
SpecificNoteToken), optionally CreateDate >= since.

ASSUMPTIONS (confirm with discovery task T013): which IDO exposes the method (idos.py) and the
property names; verified only against fakes.
"""

from . import errors, idos
from .errors import (ErpRejected, ErpUncertain, ErpUnreachable, LiveWritesDisabled, error, failure)

# record_type -> (IDO that holds the row, key property, ObjectName used by the note routines)
RECORDS = {"co": (idos.CO_IDO, "CoNum", "co"), "job": (idos.JOB_IDO, "Job", "job")}


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


def _row_pointer(soap, record_type, record_key):
    ido, key_prop, _ = RECORDS[record_type]
    rows = soap.load(ido, ["RowPointer", key_prop], idos.filter_eq(key_prop, record_key))
    return rows[0]["RowPointer"] if rows else None


def add_note(soap, record_type, record_key, text, internal=True):
    rp = _row_pointer(soap, record_type, record_key)
    if rp is None:
        return failure("add_note", error(errors.NOT_FOUND, f"{record_type} {record_key} not found"))
    ido, method = idos.create_note()
    try:
        # params: ObjectName, RowPointer, NoteDesc, NoteText, InternalFlag
        soap.invoke(ido, method, [RECORDS[record_type][2], rp, text[:60], text, 1 if internal else 0])
    except LiveWritesDisabled:
        return failure("add_note", error(errors.LIVE_WRITES_DISABLED, "live ERP writes are disabled"))
    except ErpUncertain as exc:  # the note may exist; do not retry blindly
        return failure("add_note", error(errors.UNCERTAIN_OUTCOME, str(exc)))
    except ErpRejected as exc:
        return failure("add_note", error(errors.ERP_REJECTED, str(exc)))
    except ErpUnreachable as exc:
        return failure("add_note", error(errors.ERP_UNREACHABLE, str(exc)))
    return {"ok": True, "action": "add_note"}


def get_notes(soap, record_type, record_key, since=None):
    rp = _row_pointer(soap, record_type, record_key)
    if rp is None:
        return failure("get_notes", error(errors.NOT_FOUND, f"{record_type} {record_key} not found"))
    links = soap.load(idos.OBJECT_NOTE_IDO, ["SpecificNoteToken", "NhNoteFlag"],
                      idos.filter_eq("RefRowPointer", rp))
    out = []
    for link in sorted(links, key=lambda r: int(float(r["SpecificNoteToken"]))):
        token = link["SpecificNoteToken"]
        cond = f"SpecificNoteToken = {token}" if str(token).replace(".", "", 1).isdigit() else idos.filter_eq(
            "SpecificNoteToken", token)
        if since:
            cond += f" AND CreateDate >= {idos.quote(since)}"
        for n in soap.load(idos.NOTE_IDO, ["SpecificNoteToken", "NoteContent", "NoteDesc", "CreateDate",
                                           "CreatedBy"], cond):
            out.append({"token": int(float(token)), "text": n.get("NoteContent", ""),
                        "internal": str(link.get("NhNoteFlag", "")) in ("1", "True", "true"),
                        "created_at": str(n.get("CreateDate", "")), "created_by": n.get("CreatedBy", "")})
    return {"ok": True, "action": "get_notes", "notes": out}
