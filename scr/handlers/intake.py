"""POST /confirmed-orders: validate, durably record, start the upload, return a job ID."""

import json
import time

from syteline import errors, validation
from syteline.config import worker_function_name
from syteline.jobs import JobStore, RefConflict

from ._common import log_event, response

# Overridable factories (tests inject fakes).
_store_factory = None
_lambda_factory = None


def _store():
    return _store_factory() if _store_factory else JobStore.from_env()


def _lambda():
    if _lambda_factory:
        return _lambda_factory()
    import boto3

    return boto3.client("lambda")


def _public(job, duplicate):
    return {"upload_id": job["upload_id"], "state": job["state"], "duplicate": duplicate}


def handler(event, context):
    started = time.monotonic()
    try:
        body = json.loads(event.get("body") or "")
    except (TypeError, ValueError):
        log_event("create_order", started, outcome="400")
        return response(400, {"errors": [errors.error(errors.VALIDATION, "body must be valid JSON")]})

    problems = validation.validate_order(body)
    if problems:
        log_event("create_order", started, outcome="400", request_ref=(body or {}).get("request_ref")
                  if isinstance(body, dict) else None)
        return response(400, {"errors": problems})

    identity = ((event.get("requestContext") or {}).get("identity") or {}).get("userArn") or "unknown"
    try:
        job, duplicate = _store().create_job(body, validation.payload_hash(body), body["uploaded_by"], identity)
    except RefConflict:
        log_event("create_order", started, outcome="409", request_ref=body["request_ref"])
        return response(409, {"errors": [errors.error(
            errors.REQUEST_REF_CONFLICT, "request_ref was already used with a different order")]})
    except Exception:
        # nothing was recorded, so the caller must keep the order and retry
        log_event("create_order", started, outcome="503", request_ref=body["request_ref"])
        return response(503, {"errors": [errors.error(errors.STORE_UNAVAILABLE, "could not record the order")]})

    if duplicate:
        log_event("create_order", started, outcome="200", upload_id=job["upload_id"], duplicate=True,
                  uploaded_by=body["uploaded_by"], caller=identity)
        return response(200, _public(job, True))

    try:  # async, no retries: a failure here leaves the job `accepted` and the sweeper re-drives it
        _lambda().invoke(FunctionName=worker_function_name(), InvocationType="Event",
                         Payload=json.dumps({"upload_id": job["upload_id"]}).encode("utf-8"))
    except Exception:
        pass
    log_event("create_order", started, outcome="202", upload_id=job["upload_id"], request_ref=body["request_ref"],
              uploaded_by=body["uploaded_by"], caller=identity)
    return response(202, _public(job, False))
