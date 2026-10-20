"""GET /upload-jobs/{id}: report the state of an upload."""

import time

from syteline import errors
from syteline.jobs import JobStore

from ._common import log_event, response

_store_factory = None


def _store():
    return _store_factory() if _store_factory else JobStore.from_env()


def _view(job):
    out = {"job_id": job["job_id"], "request_ref": job["request_ref"], "uploaded_by": job["uploaded_by"],
           "state": job["state"], "created_at": job["created_at"], "updated_at": job["updated_at"]}
    rec = job.get("erp_records") or {}
    if job["state"] == "succeeded":
        out["co_num"] = rec.get("co_num")
        out["jobs"] = rec.get("lines", [])
    if job["state"] in ("failed", "needs_reconciliation") and job.get("failure"):
        out["failure"] = job["failure"]
    if job["state"] == "needs_reconciliation":
        out["possibly_written"] = rec.get("possibly_written", [])
    return out


def handler(event, context):
    started = time.monotonic()
    job_id = ((event.get("pathParameters") or {}).get("id") or "").strip()
    job = _store().get_job(job_id) if job_id else None
    if job is None:
        log_event("get_status", started, outcome="404", job_id=job_id)
        return response(404, {"errors": [errors.error(errors.NOT_FOUND, "no such upload job")]})
    log_event("get_status", started, outcome="200", job_id=job_id, state=job["state"])
    return response(200, _view(job))
