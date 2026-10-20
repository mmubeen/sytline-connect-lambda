"""Runs the upload pipeline for one accepted job. Never replays: only `accepted` jobs are claimed."""

import time

from syteline import config, errors, pipeline
from syteline.client import SoapIdoClient
from syteline.jobs import JobStore

from ._common import log_event

# Overridable factories (tests inject fakes).
_store_factory = None
_clients_factory = None  # () -> (soap, cfg)


def _store():
    return _store_factory() if _store_factory else JobStore.from_env()


def _clients():
    if _clients_factory:
        return _clients_factory()
    cfg = config.load_config()
    return SoapIdoClient(cfg, timeout=config.timeout(), verify=config.verify_tls()), cfg


def _remaining(context):
    fn = getattr(context, "get_remaining_time_in_millis", None)
    return fn if callable(fn) else (lambda: 10 ** 9)


def handler(event, context):
    started = time.monotonic()
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
