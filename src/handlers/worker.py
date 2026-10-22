"""Runs the upload pipeline for one accepted job. Never replays: only `accepted` jobs are claimed."""

import time

from syteline import config, errors, idp, pipeline, savewf
from syteline.errors import IntegrationError
from syteline.client import SoapIdoClient
from syteline.jobs import JobStore
from syteline.reservations import ReservationStore

from ._common import log_event

# Overridable factories (tests inject fakes).
_store_factory = None
_reservation_store_factory = None
_clients_factory = None  # () -> (soap, cfg)


def _store():
    return _store_factory() if _store_factory else JobStore.from_env()


def _reservations():
    return _reservation_store_factory() if _reservation_store_factory else ReservationStore.from_env()


def _clients():
    if _clients_factory:
        return _clients_factory()
    cfg = config.load_config()
    return SoapIdoClient(cfg, timeout=config.timeout(), verify=config.verify_tls()), cfg


def _remaining(context):
    fn = getattr(context, "get_remaining_time_in_millis", None)
    return fn if callable(fn) else (lambda: 10 ** 9)


MAX_SAVE_ATTEMPTS = 5


def _document_stop(store, reservation_id, item, code, message, started):
    """The document could not be used when the save ran; nothing was written. An unavailable IDP source is
    retried later (the sweeper starts the save again) up to MAX_SAVE_ATTEMPTS; anything else ends the save."""
    if code == errors.IDP_UNAVAILABLE and int(item.get("attempts", 0)) < MAX_SAVE_ATTEMPTS and \
            store.release_claim(reservation_id):
        log_event("save", started, reservation_id=reservation_id, outcome="retry_later", code=code)
        return {"state": "save_accepted"}
    store.fail_save(reservation_id, code, message, "document")
    log_event("save", started, reservation_id=reservation_id, outcome="failed", code=code, step="document")
    return {"state": "failed"}


def save_handler(reservation_id, context, started):
    """Stage 2 of the job-number workflow: save the order information using the reserved numbers."""
    store = _reservations()
    if not reservation_id or not store.claim(reservation_id):
        log_event("save", started, reservation_id=reservation_id, outcome="skipped")
        return {"skipped": True}
    item = store.get(reservation_id)

    def progress(possibly, res):
        store.record_progress(reservation_id, possibly, res)

    try:
        params = item["save"]
        try:  # the PO details are read from IDP now, never from our table (FR-021)
            doc = idp.po_details(params["document_id"])
        except IntegrationError as exc:
            return _document_stop(store, reservation_id, item, exc.err["code"], exc.err["message"], started)
        if doc["fingerprint"] != item["fingerprint"]:
            return _document_stop(store, reservation_id, item, errors.DOCUMENT_CHANGED,
                                  "the document no longer matches the reservation", started)
        request = dict(doc["details"])
        for k in ("order_date", "received_date", "warehouse", "order_type"):
            if params.get(k):
                request[k] = params[k]
        soap, cfg = _clients()
        outcome = savewf.run_save(item, request, soap, cfg, _remaining(context), on_progress=progress)
    except Exception as exc:  # unexpected: we cannot know what was written, so never auto-retry
        current = store.get(reservation_id) or {}
        possibly = (current.get("erp_records") or {}).get("possibly_written", [])
        if possibly:
            store.park(reservation_id, errors.UNCERTAIN_OUTCOME, f"unexpected error: {type(exc).__name__}", "worker",
                       possibly, current.get("results"))
            state = "needs_reconciliation"
        else:
            store.fail_save(reservation_id, errors.UNCERTAIN_OUTCOME, f"unexpected error: {type(exc).__name__}",
                            "worker", current.get("results"))
            state = "failed"
        log_event("save", started, reservation_id=reservation_id, outcome=state, error=type(exc).__name__)
        return {"state": state}

    state = outcome["state"]
    res = outcome["results"]
    warnings = outcome.get("warnings")
    if state == "succeeded":
        store.finish_saved(reservation_id, res, warnings)
        state = "saved"
    elif state == "partly_saved":
        store.finish_partly_saved(reservation_id, outcome["code"], outcome["message"], outcome["step"], res, warnings)
    elif state == "failed":
        store.fail_save(reservation_id, outcome["code"], outcome["message"], outcome["step"], res, warnings)
    else:
        store.park(reservation_id, outcome["code"], outcome["message"], outcome["step"],
                   outcome.get("possibly_written", []), res, warnings=warnings)
    log_event("save", started, reservation_id=reservation_id, outcome=state, co_num=outcome.get("co_num"),
              code=outcome.get("code"), step=outcome.get("step"))
    return {"state": state}


def handler(event, context):
    started = time.monotonic()
    if (event or {}).get("reservation_id"):
        return save_handler(event["reservation_id"], context, started)
    upload_id = (event or {}).get("upload_id")
    store = _store()
    if not upload_id or not store.claim(upload_id):
        log_event("upload", started, upload_id=upload_id, outcome="skipped")
        return {"skipped": True}
    job = store.get_job(upload_id)
    try:
        soap, cfg = _clients()
        outcome = pipeline.run_upload(
            job["request"], soap, cfg, _remaining(context),
            on_progress=lambda possibly, cust: store.record_progress(upload_id, possibly, cust))
    except Exception as exc:  # unexpected: we cannot know what was written, so never auto-retry
        current = store.get_job(upload_id) or {}
        possibly = (current.get("erp_records") or {}).get("possibly_written", [])
        if possibly:
            store.park(upload_id, errors.UNCERTAIN_OUTCOME, f"unexpected error: {type(exc).__name__}",
                       "worker", possibly)
            state = "needs_reconciliation"
        else:
            store.fail(upload_id, errors.UNCERTAIN_OUTCOME, f"unexpected error: {type(exc).__name__}", "worker")
            state = "failed"
        log_event("upload", started, upload_id=upload_id, outcome=state, error=type(exc).__name__)
        return {"state": state}

    cust = outcome.get("customer_match")
    if outcome["state"] == "succeeded":
        store.succeed(upload_id, outcome["co_num"], outcome["lines"], cust)
    elif outcome["state"] == "failed":
        store.fail(upload_id, outcome["code"], outcome["message"], outcome["step"], cust)
    else:
        store.park(upload_id, outcome["code"], outcome["message"], outcome["step"],
                   outcome.get("possibly_written", []), customer_match=cust)
    log_event("upload", started, upload_id=upload_id, outcome=outcome["state"], co_num=outcome.get("co_num"),
              code=outcome.get("code"), step=outcome.get("step"))
    return {"state": outcome["state"]}
