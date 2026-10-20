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
    log.info(json.dumps({"type": kind, "ms": int((time.monotonic() - started) * 1000), **fields}, default=str))
