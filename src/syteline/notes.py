"""add_note / get_notes on CO and job records, through the web service only (FR-018; research R10).

add_note: load the record's RowPointer, then call CreateSpecificNoteSp as a web-service method (it
creates AND attaches the note; a write, gated by SYTELINE_LIVE_WRITES).
get_notes: read ObjectNotes (RefRowPointer = the record's RowPointer) and SpecificNotes (joined on
SpecificNoteToken), optionally CreateDate >= since.

ASSUMPTIONS (confirm with discovery task T013): which IDO exposes the method (idos.py) and the
property names; verified only against fakes.
"""

import logging

from . import config, errors, idos, rowbuild
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


# -- the document's notes on the customer order (feature 011) ----------------------------------------------------------------
log = logging.getLogger(__name__)


def _token_of(row):
    token = str((row or {}).get("SpecificNoteToken", "") or "").strip()
    return token if token and token != "0" else ""


def _existing(soap, pointer):
    """{title: (token, content)} of the notes already tied to the order (link rows with the order's header token, then their notes)."""
    links = soap.load(idos.object_note_link_ido(), ["SpecificNoteToken"],
                      f"RefRowPointer = {idos.quote(pointer)} AND NoteHeaderToken = {idos.NOTE_HEADER_CO}")
    found = {}
    for link in links:
        token = _token_of(link)
        if not token:
            continue
        cond = f"SpecificNoteToken = {token}" if token.isdigit() else idos.filter_eq("SpecificNoteToken", token)
        for n in soap.load(idos.SPECIFIC_NOTE_IDO, ["SpecificNoteToken", "NoteDesc", "NoteContent"], cond, record_cap=1):
            found.setdefault(str(n.get("NoteDesc", "")).strip(), (token, str(n.get("NoteContent", ""))))
    return found


def attach_order_notes(soap, co_num, sections, replace=False, time_left=None):
    """Attach the document's text sections to the order `co_num`, one note each (feature 011).

    The ERP owner's path: the order's RowPointer (SLCos), a SpecificNotes row (NoteDesc = title, NoteContent = text), its
    SpecificNoteToken, then the link row (SpecificNoteToken, NoteHeaderToken 31, RefRowPointer, NoteType 0). A title the order already
    has is skipped; with `replace` (development mode) its text is updated instead. A refused note is reported and the next is still
    tried; an unknown outcome or an unreachable ERP stops the rest, and nothing is retried. NEVER raises, never reports note text.

    Returns {"attempted", "attached": [title], "not_attached": [{"title", "reason", "uncertain"}], "skipped_existing", "replaced"}.
    """
    report = {"attempted": False, "attached": [], "not_attached": [], "skipped_existing": [], "replaced": []}
    sections = [s for s in sections or [] if str(s.get("text", "")).strip()]
    if not sections:
        return report
    if not config.live_writes_enabled():
        report["not_attached"] = [{"title": s["title"], "reason": "live ERP writes are disabled", "uncertain": False} for s in sections]
        return report
    report["attempted"] = True

    def stop_rest(index, reason, uncertain):
        for k, s in enumerate(sections[index:]):
            report["not_attached"].append({"title": s["title"], "uncertain": uncertain and k == 0,
                                           "reason": reason if k == 0 else "not attempted: " + reason})

    try:
        rows = soap.load(idos.CO_IDO, ["RowPointer"], rowbuild.co_filter(co_num), record_cap=1)
        pointer = str((rows[0].get("RowPointer") if rows else "") or "").strip()
        if not pointer:
            stop_rest(0, f"order {co_num} has no RowPointer to attach notes to", False)
            return report
        existing = _existing(soap, pointer)
    except (ErpRejected, ErpUncertain, ErpUnreachable) as exc:
        stop_rest(0, f"could not read the order's notes: {str(exc)[:200]}", False)
        return report
    for index, sec in enumerate(sections):
        title, text = sec["title"], str(sec["text"]).strip()
        if time_left is not None and time_left() < 5000:
            stop_rest(index, "not enough Lambda time left", False)
            return report
        try:
            if title in existing:
                if not replace:
                    report["skipped_existing"].append(title)
                    continue
                token = existing[title][0]
                key = {"SpecificNoteToken": int(token) if token.isdigit() else token}
                soap.update(idos.SPECIFIC_NOTE_IDO, key, {"NoteContent": text})
                report["replaced"].append(title)
                continue
            stored = soap.insert(idos.SPECIFIC_NOTE_IDO, {"NoteDesc": title, "NoteContent": text})
            token = _token_of(stored)
            if not token:  # the answer carried no token: find the note just written, by title and text
                found = soap.load(idos.SPECIFIC_NOTE_IDO, ["SpecificNoteToken"],
                                  f"NoteDesc = {idos.quote(title)} AND NoteContent = {idos.quote(text)}", record_cap=1)
                token = _token_of(found[0] if found else None)
            if not token:
                report["not_attached"].append({"title": title, "uncertain": True,
                                               "reason": "the note was written but its token could not be found, so it was not linked"})
                continue
            soap.insert(idos.object_note_link_ido(), {"SpecificNoteToken": token, "NoteHeaderToken": idos.NOTE_HEADER_CO,
                                                      "RefRowPointer": pointer, "NoteType": idos.NOTE_TYPE})
            report["attached"].append(title)
        except ErpRejected as exc:
            report["not_attached"].append({"title": title, "uncertain": False, "reason": f"ERP refused: {str(exc)[:200]}"})
        except ErpUncertain as exc:
            stop_rest(index, f"outcome unknown: {str(exc)[:200]}", True)
            return report
        except ErpUnreachable as exc:
            stop_rest(index, f"ERP unreachable: {str(exc)[:200]}", False)
            return report
        except LiveWritesDisabled:
            stop_rest(index, "live ERP writes are disabled", False)
            return report
    return report
