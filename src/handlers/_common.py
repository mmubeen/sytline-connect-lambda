"""Shared helpers for the API Gateway handlers."""

import json
import logging
import time

log = logging.getLogger()
log.setLevel(logging.INFO)


def response(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, default=str)}


def log_event(kind, started, **fields):
    """One structured line per request: type, record identifiers, outcome, duration (FR-011). No secrets."""
    from syteline import devmode  # late: keeps the module import light

    if devmode.active():
        fields.setdefault("dev_target", devmode.active())
    log.info(json.dumps({"type": kind, "ms": int((time.monotonic() - started) * 1000), **fields}, default=str))


def caller_identity(event):
    """The calling principal (API Gateway `userArn`) or "unknown".

    For an assumed role (arn:...:assumed-role/<Role>/<session>) the session name is dropped, so the same role
    is recognized across sessions (reservations belong to the caller that made them).
    """
    arn = ((event.get("requestContext") or {}).get("identity") or {}).get("userArn") or "unknown"
    parts = arn.split(":", 5)
    if len(parts) == 6 and parts[5].startswith("assumed-role/"):
        pieces = parts[5].split("/")
        if len(pieces) >= 3:
            parts[5] = "/".join(pieces[:2])
            return ":".join(parts)
    return arn


DOCUMENT_STATUS = {"DOCUMENT_NOT_FOUND": 404, "DOCUMENT_NOT_READY": 409, "DOCUMENT_INCOMPLETE": 422,
                   "DOCUMENT_NEEDS_REVIEW": 422, "DOCUMENT_CHANGED": 422, "TOO_MANY_LINES": 422,
                   "IDP_UNAVAILABLE": 502}


def document_refusal(exc):
    """The HTTP response for a refusal raised while reading the IDP document (nothing was reserved or written)."""
    body = {"errors": [exc.err]}
    body.update(exc.extra)  # state / fields
    return response(DOCUMENT_STATUS.get(exc.err["code"], 502), body)
