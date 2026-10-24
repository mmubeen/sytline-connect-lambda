"""The entry log line (specs/006, FR-016): one JSON line holding the event, written on entry to the handler.

By default the line is a REDACTED copy: the values of `input`, `output`, `body` and `text` (where page text, order bodies and note
text arrive) are replaced by their size, and for an IDP event the `input` also carries the document ID. With the opt-in setting
LOG_EVENTS=true the whole event is logged. Either way header credentials (Authorization, X-Amz-Security-Token, Cookie, X-Api-Key) are
masked, and an event over the cap (LOG_EVENTS_MAX_CHARS, default 8000) is cut so the line stays one valid JSON object.

`log_entry` never raises: it must not change what the function does.
"""

import json
import logging
import os

from . import eventdoc

REDACT_KEYS = frozenset({"input", "output", "body", "text"})
MASK_HEADERS = frozenset({"authorization", "x-amz-security-token", "cookie", "x-api-key"})
DEFAULT_CAP = 8000
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


def _cap(environ):
    try:
        cap = int(environ.get("LOG_EVENTS_MAX_CHARS", ""))
        return cap if cap > 0 else DEFAULT_CAP
    except ValueError:
        return DEFAULT_CAP


def render(event, environ=None):
    """The entry line, as a JSON string."""
    environ = os.environ if environ is None else environ
    whole = str(environ.get("LOG_EVENTS", "")).strip().lower() == "true"
    obj, described = _as_object(event)
    shown = described if obj is None else _walk(obj, redact=not whole)
    text = json.dumps(shown, default=str)
    cap = _cap(environ)
    if len(text) > cap:
        shown = {"truncated": True, "chars": len(text), "head": text[:cap]}
    return json.dumps({"type": "event_entry", "mode": "whole" if whole else "redacted", "event": shown}, default=str)


def log_entry(event, logger=None, environ=None):
    logger = logger or logging.getLogger()
    try:
        logger.info(render(event, environ))
    except Exception as exc:  # a logging problem must never become a failed invocation
        try:
            logger.warning("event_entry could not be built: %s", type(exc).__name__)
        except Exception:
            pass
