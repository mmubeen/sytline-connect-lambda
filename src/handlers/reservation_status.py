"""GET /job-reservations/{id}: report the state of a reservation and its save."""

import time

from syteline import errors
from syteline.reservations import NEEDS_RECONCILIATION, ReservationStore, effective_state, numbers_report

from ._common import log_event, response

_store_factory = None


def _store():
    return _store_factory() if _store_factory else ReservationStore.from_env()


def _line(r):
    out = {"line": int(r["line"]), "job": str(r["job"]), "status": r["status"], "warnings": list(r.get("warnings") or [])}
    for k in ("co_line", "item", "error", "action", "due_date", "due_source"):
        if k in r:
            out[k] = int(r[k]) if k == "co_line" else r[k]
    return out


def _view(item, now):
    state = effective_state(item, now)
    report = numbers_report(item, now)
    out = {"reservation_id": item["reservation_id"], "request_ref": item["request_ref"],
           "document_id": item["document_id"], "uploaded_by": item["uploaded_by"], "state": state,
           "co_num": report["co_num"], "jobs": report["jobs"],
           "created_at": int(item["created_at"]), "updated_at": int(item["updated_at"])}
    if item.get("dev_target"):  # a development-mode run (DEV_TARGET_CO): the order is overwritten, not created
        out["development"] = {"target": item["dev_target"]}
    if item.get("save"):  # a save was accepted: per-line results (empty until the run records some)
        out["lines"] = [_line(r) for r in item.get("results") or []]
    if item.get("warnings"):  # order-level warnings from the save (for example: customer defaults not available)
        out["warnings"] = list(item["warnings"])
    if item.get("failure"):
        out["failure"] = item["failure"]
    elif state != item["state"]:  # a reserve run that never finished
        out["failure"] = {"code": errors.UNCERTAIN_OUTCOME, "message": "the reservation never finished",
                          "step": "reserve"}
    if state == NEEDS_RECONCILIATION:
        out["possibly_written"] = (item.get("erp_records") or {}).get("possibly_written", [])
    return out


def handler(event, context):
    started = time.monotonic()
    rid = ((event.get("pathParameters") or {}).get("id") or "").strip()
    item = _store().get(rid) if rid else None
    if item is None:
        log_event("reservation_status", started, outcome="404", reservation_id=rid)
        return response(404, {"errors": [errors.error(errors.NOT_FOUND, "no such reservation")]})
    view = _view(item, int(time.time()))
    log_event("reservation_status", started, outcome="200", reservation_id=rid, state=view["state"])
    return response(200, view)
