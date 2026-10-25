"""The entry log line (specs/006, FR-016): one JSON line holding the event, written on entry to the handler.

By default the line is a REDACTED copy: the values of `input`, `output`, `body` and `text` (where page text, order bodies and note
text arrive) are replaced by their size, and for an IDP event the `input` also carries the document ID. With the opt-in setting
LOG_EVENTS=true the whole event is logged, with no cap. Either way header credentials (Authorization, X-Amz-Security-Token, Cookie,
X-Api-Key) are masked.

One log entry holds at most 256 KB, so a line over MAX_LINE_BYTES is replaced by the start of the event ("head") and, in whole mode,
the full masked event is written to the S3 bucket named by EVENT_COPY_BUCKET (created by the stack while LOG_EVENTS is true) with its
location in the line ("copy"); a failed or impossible copy shows as "copy_error". Nothing here may change what the function does.

`log_entry` never raises: it must not change what the function does.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

from . import eventdoc

REDACT_KEYS = frozenset({"input", "output", "body", "text"})
MASK_HEADERS = frozenset({"authorization", "x-amz-security-token", "cookie", "x-api-key"})
MAX_LINE_BYTES = 200_000   # one log entry holds 256 KB; the rest is room for the platform's prefix and JSON escaping
HEAD_START_BYTES = 100_000
_s3_factory = None          # tests inject a fake S3 client
MASKED = "***"


def _size(value):
    return len(value) if isinstance(value, str) else len(json.dumps(value, default=str))


def _document_id(text):
    """The document id inside an execution's `input` text, or None (the parser of the intake is not needed for a log line)."""
    try:
        doc = json.loads(text)["document"]["id"]
    except (ValueError, KeyError, TypeError):
        return None
    return doc if isinstance(doc, str) and eventdoc.DOCUMENT_ID.match(doc) else None


def _mask_headers(value):
    if not isinstance(value, dict):
        return value
    return {k: (MASKED if str(k).lower() in MASK_HEADERS else v) for k, v in value.items()}


def _walk(node, redact, in_detail=False):
    """A copy of `node` with header credentials masked and, if `redact`, the values of REDACT_KEYS replaced by their size."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if str(key).lower() in ("headers", "multivalueheaders"):
                out[key] = _mask_headers(value)
            elif redact and key in REDACT_KEYS:
                marker = {"redacted": True, "chars": _size(value)}
                if key == "input" and in_detail and isinstance(value, str):
                    doc = _document_id(value)
                    if doc:
                        marker["document_id"] = doc
                out[key] = marker
            else:
                out[key] = _walk(value, redact, in_detail=(key == "detail"))
        return out
    if isinstance(node, list):
        return [_walk(item, redact) for item in node]
    return node


def _as_object(event):
    """(object, None) for something that is, or holds, a JSON object; else (None, description)."""
    if isinstance(event, (bytes, bytearray)):
        try:
            event = bytes(event).decode("utf-8")
        except UnicodeDecodeError:
            return None, {"payload_type": "bytes", "chars": len(event)}
    if isinstance(event, str):
        try:
            parsed = json.loads(event)
        except ValueError:
            return None, {"payload_type": "str", "chars": len(event)}
        return (parsed, None) if isinstance(parsed, dict) else (None, {"payload_type": "str", "chars": len(event)})
    if isinstance(event, dict):
        return event, None
    return None, {"payload_type": type(event).__name__, "chars": 0 if event is None else _size(event)}


def _s3():
    if _s3_factory:
        return _s3_factory()
    import boto3
    from botocore.config import Config

    return boto3.client("s3", config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}))


def put_event_copy(text, environ):
    """Write the full masked event to the event-copy bucket: {"copy": {"bucket", "key"}} or {"copy_error": why}."""
    bucket = str(environ.get("EVENT_COPY_BUCKET", "")).strip()
    if not bucket:
        return {"copy_error": "not_configured"}
    try:
        now = datetime.now(timezone.utc)
        key = f"events/{now:%Y/%m/%d}/{now:%H%M%S}-{uuid.uuid4()}.json"
        _s3().put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"), ContentType="application/json",
                         ServerSideEncryption="AES256")
        return {"copy": {"bucket": bucket, "key": key}}
    except Exception as exc:  # a missing route, a denied write, a bad bucket: shown in the line, never raised
        return {"copy_error": type(exc).__name__}


def _line(whole, shown):
    return json.dumps({"type": "event_entry", "mode": "whole" if whole else "redacted", "event": shown}, default=str)


def _head(text, whole, extra):
    """The oversized line: the start of the event JSON, shortened until the finished line fits."""
    raw = text.encode("utf-8")
    size = min(HEAD_START_BYTES, len(raw))
    while True:
        head = raw[:size].decode("utf-8", errors="ignore")
        line = _line(whole, {"truncated": True, "chars": len(text), "head": head, **extra})
        if len(line.encode("utf-8")) <= MAX_LINE_BYTES or size <= 1:
            return line
        size //= 2


def render(event, environ=None):
    """The entry line, as a JSON string."""
    environ = os.environ if environ is None else environ
    whole = str(environ.get("LOG_EVENTS", "")).strip().lower() == "true"
    obj, described = _as_object(event)
    shown = described if obj is None else _walk(obj, redact=not whole)
    line = _line(whole, shown)
    if len(line.encode("utf-8")) <= MAX_LINE_BYTES:
        return line
    text = json.dumps(shown, default=str)
    return _head(text, whole, put_event_copy(text, environ) if whole else {})


def log_entry(event, logger=None, environ=None):
    logger = logger or logging.getLogger()
    try:
        logger.info(render(event, environ))
    except Exception as exc:  # a logging problem must never become a failed invocation
        try:
            logger.warning("event_entry could not be built: %s", type(exc).__name__)
        except Exception:
            pass
