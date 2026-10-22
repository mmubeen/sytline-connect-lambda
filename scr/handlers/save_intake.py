"""POST /job-reservations/{id}/save: accept the PO details, start the save with the reserved numbers."""

import json
import time

from syteline import errors, validation
from syteline.config import worker_function_name
from syteline.reservations import RESERVED, SAVE_ACCEPTED, ReservationStore

from ._common import caller_identity, log_event, response

# Overridable factories (tests inject fakes).
_store_factory = None
_lambda_factory = None


def _store():
    return _store_factory() if _store_factory else ReservationStore.from_env()


def _lambda():
    if _lambda_factory:
        return _lambda_factory()
    import boto3

    return boto3.client("lambda")


def _state_refusal(state):
    """The 409 body for a reservation that cannot be saved now, or None when it can (state `reserved`)."""
    if state == RESERVED:
        return None
    code = errors.RESERVATION_NOT_READY if state in ("reserving", "failed") else errors.RESERVATION_USED
    msg = ("the reservation is not ready to be saved (it is %s)" % state if code == errors.RESERVATION_NOT_READY
           else "this reservation was already used (it is %s); a reservation can be saved once" % state)
    return {"state": state, "errors": [errors.error(code, msg)]}


def handler(event, context):
    started = time.monotonic()
    rid = ((event.get("pathParameters") or {}).get("id") or "").strip()
    try:
        body = json.loads(event.get("body") or "")
    except (TypeError, ValueError):
        log_event("save_intake", started, outcome="400", reservation_id=rid)
        return response(400, {"errors": [errors.error(errors.VALIDATION, "body must be valid JSON")]})

    problems = validation.validate_save(body)
    if problems:
        log_event("save_intake", started, outcome="400", reservation_id=rid)
        return response(400, {"errors": problems})

    try:
        store = _store()
        item = store.get(rid) if rid else None
    except Exception:
        log_event("save_intake", started, outcome="503", reservation_id=rid)
        return response(503, {"errors": [errors.error(errors.STORE_UNAVAILABLE, "could not read the reservation")]})
    if item is None:
        log_event("save_intake", started, outcome="404", reservation_id=rid)
        return response(404, {"errors": [errors.error(errors.NOT_FOUND, "no such reservation")]})

    caller = caller_identity(event)
    # Refusals, all before any ERP access (FR-009): wrong owner, not usable, wrong PO.
    if item["caller_identity"] != caller or item["uploaded_by"] != body["uploaded_by"]:
        log_event("save_intake", started, outcome="403", reservation_id=rid)
        return response(403, {"errors": [errors.error(
            errors.RESERVATION_FORBIDDEN, "this reservation was made by a different caller or operator")]})
    refusal = _state_refusal(item["state"])
    if refusal:
        log_event("save_intake", started, outcome="409", reservation_id=rid, state=item["state"])
        return response(409, refusal)
    if validation.fingerprint(body["customer"], body["po_number"], len(body["lines"])) != item["fingerprint"]:
        log_event("save_intake", started, outcome="422", reservation_id=rid)
        return response(422, {"errors": [errors.error(
            errors.RESERVATION_MISMATCH,
            "the customer, PO number or number of lines differ from the reservation")]})

    try:
        accepted = store.accept_save(rid, validation.payload_hash(body), body)
    except Exception:
        # nothing was recorded, so the reservation is still usable
        log_event("save_intake", started, outcome="503", reservation_id=rid)
        return response(503, {"errors": [errors.error(errors.STORE_UNAVAILABLE, "could not record the save request")]})
    if not accepted:  # lost a race with another save of the same reservation
        current = store.get(rid) or item
        log_event("save_intake", started, outcome="409", reservation_id=rid, state=current["state"])
        return response(409, _state_refusal(current["state"]) or _state_refusal(SAVE_ACCEPTED))

    try:  # async, no retries: a failure here leaves the reservation `save_accepted` and the sweeper re-drives it
        _lambda().invoke(FunctionName=worker_function_name(), InvocationType="Event",
                         Payload=json.dumps({"reservation_id": rid}).encode("utf-8"))
    except Exception:
        pass
    log_event("save_intake", started, outcome="202", reservation_id=rid, uploaded_by=body["uploaded_by"], caller=caller)
    return response(202, {"reservation_id": rid, "state": SAVE_ACCEPTED})
