"""Scheduled: re-drive `accepted` jobs (nothing can have been written) and park stale `running` ones.

The same rule applies to job-number reservations: `save_accepted` saves are re-driven, stale `saving` ones are
parked as needs_reconciliation; `needs_reconciliation` and `partly_saved` are never replayed.
"""

import json
import os
import time

from syteline import errors
from syteline.config import worker_function_name
from syteline.jobs import JobStore
from syteline.reservations import ReservationStore

from ._common import log_event

_store_factory = None
_reservation_store_factory = None
_lambda_factory = None


def _store():
    return _store_factory() if _store_factory else JobStore.from_env()


def _reservations():
    return _reservation_store_factory() if _reservation_store_factory else ReservationStore.from_env()


def _lambda():
    if _lambda_factory:
        return _lambda_factory()
    import boto3

    return boto3.client("lambda")


def handler(event, context):
    started = time.monotonic()
    redrive_after = int(os.environ.get("SWEEPER_REDRIVE_AFTER_SECONDS", "120"))
    stale_after = int(os.environ.get("WORKER_TIMEOUT_SECONDS", "300")) + 30
    store, lam = _store(), _lambda()
    redriven, parked = [], []
    for job in store.list_accepted_older_than(redrive_after):
        try:
            lam.invoke(FunctionName=worker_function_name(), InvocationType="Event",
                       Payload=json.dumps({"upload_id": job["upload_id"]}).encode("utf-8"))
            redriven.append(job["upload_id"])
        except Exception:
            pass  # still accepted; the next sweep tries again
    for job in store.list_running_older_than(stale_after):
        possibly = (job.get("erp_records") or {}).get("possibly_written", [])
        if store.park(job["upload_id"], errors.STALE_RUNNING,
                      "worker stopped before finishing; check SyteLine for records from this upload",
                      "sweeper", possibly):
            parked.append(job["upload_id"])

    rstore = _reservations()
    for res in rstore.list_save_accepted_older_than(redrive_after):
        try:
            lam.invoke(FunctionName=worker_function_name(), InvocationType="Event",
                       Payload=json.dumps({"reservation_id": res["reservation_id"]}).encode("utf-8"))
            redriven.append(res["reservation_id"])
        except Exception:
            pass  # still save_accepted; the next sweep tries again
    for res in rstore.list_saving_older_than(stale_after):
        possibly = (res.get("erp_records") or {}).get("possibly_written", [])
        if rstore.park(res["reservation_id"], errors.STALE_RUNNING,
                       "worker stopped before finishing; check SyteLine for records from this save",
                       "sweeper", possibly, res.get("results")):
            parked.append(res["reservation_id"])
    log_event("sweep", started, redriven=len(redriven), parked=len(parked))
    return {"redriven": redriven, "parked": parked}
