"""Runs the upload pipeline for one accepted job. Never replays: only `accepted` jobs are claimed."""

import time

from syteline import config, errors, pipeline
from syteline.client import SoapIdoClient
from syteline.jobs import JobStore
from syteline.sql import SqlClient

from ._common import log_event

# Overridable factories (tests inject fakes).
_store_factory = None
_clients_factory = None  # () -> (sql, soap, cfg)


def _store():
    return _store_factory() if _store_factory else JobStore.from_env()


def _clients():
    if _clients_factory:
        return _clients_factory()
    cfg = config.load_config()
    return SqlClient(cfg), SoapIdoClient(cfg, timeout=config.timeout(), verify=config.verify_tls()), cfg


def _remaining(context):
    fn = getattr(context, "get_remaining_time_in_millis", None)
    return fn if callable(fn) else (lambda: 10 ** 9)


def handler(event, context):
    started = time.monotonic()
    job_id = (event or {}).get("job_id")
    store = _store()
    if not job_id or not store.claim(job_id):
        log_event("upload", started, job_id=job_id, outcome="skipped")
        return {"skipped": True}
    job = store.get_job(job_id)
    sql = None
    try:
        sql, soap, cfg = _clients()
        outcome = pipeline.run_upload(
            job["request"], sql, soap, cfg, _remaining(context),
            on_progress=lambda possibly, cust: store.record_progress(job_id, possibly, cust))
    except Exception as exc:  # unexpected: we cannot know what was written, so never auto-retry
        current = store.get_job(job_id) or {}
        possibly = (current.get("erp_records") or {}).get("possibly_written", [])
        if possibly:
            store.park(job_id, errors.UNCERTAIN_OUTCOME, f"unexpected error: {type(exc).__name__}",
                       "worker", possibly)
            state = "needs_reconciliation"
        else:
            store.fail(job_id, errors.UNCERTAIN_OUTCOME, f"unexpected error: {type(exc).__name__}", "worker")
            state = "failed"
        log_event("upload", started, job_id=job_id, outcome=state, error=type(exc).__name__)
        return {"state": state}
    finally:
        if sql is not None and hasattr(sql, "close"):
            sql.close()

    cust = outcome.get("customer_match")
    if outcome["state"] == "succeeded":
        store.succeed(job_id, outcome["co_num"], outcome["lines"], cust)
    elif outcome["state"] == "failed":
        store.fail(job_id, outcome["code"], outcome["message"], outcome["step"], cust)
    else:
        store.park(job_id, outcome["code"], outcome["message"], outcome["step"],
                   outcome.get("possibly_written", []), customer_match=cust)
    log_event("upload", started, job_id=job_id, outcome=outcome["state"], co_num=outcome.get("co_num"),
              code=outcome.get("code"), step=outcome.get("step"))
    return {"state": outcome["state"]}
