"""The processing status change event of the IDP state machine: which document does it name? (specs/006, research R2)

`parse(event)` takes the event as an object or JSON text and returns one of
  {"action": "ignore", "reason": ...}                          the event is not a finished-processing event
  {"action": "process", "document_id", "execution", "event_date"}
or raises IntegrationError(EVENT_INVALID) when it is such an event but the document cannot be identified.
Only the document ID, the execution name and the event time are used; nothing else in the event is trusted or kept.
"""

import datetime
import json
import re

from . import errors
from .errors import IntegrationError

SOURCE = "aws.states"
DETAIL_TYPE = "Step Functions Execution Status Change"
SUCCEEDED = "SUCCEEDED"
DOCUMENT_ID = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")
MAX_EXECUTION = 256


def _invalid(message):
    return IntegrationError(errors.EVENT_INVALID, message, step="event")


def _object(value, name):
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise _invalid(f"detail.{name} is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise _invalid(f"detail.{name} is not a JSON object")
    return parsed


def _document_id(detail, name):
    doc = (_object(detail.get(name), name) or {}).get("document")
    doc_id = doc.get("id") if isinstance(doc, dict) else None
    return doc_id if isinstance(doc_id, str) and doc_id else None


def _event_date(event, today=None):
    stamp = str(event.get("time") or "")[:10]
    try:
        datetime.date.fromisoformat(stamp)
        return stamp
    except ValueError:
        return (today or datetime.datetime.now(datetime.timezone.utc).date()).isoformat()


def _as_object(value):
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def unwrap(payload):
    """The event itself, or the value of an `event` key holding it (an object or JSON text)."""
    obj = _as_object(payload)
    if obj is not None and "event" in obj:
        inner = _as_object(obj["event"])
        if inner is not None:
            return inner
    return payload


def is_status_change(event):
    """True for an IDP state machine processing status change event (any status); False for any other payload."""
    obj = _as_object(event)
    return bool(obj) and obj.get("source") == SOURCE and obj.get("detail-type") == DETAIL_TYPE


def parse(event, today=None):
    if isinstance(event, (str, bytes)):
        try:
            event = json.loads(event)
        except ValueError:
            raise _invalid("the event is not valid JSON") from None
    if not isinstance(event, dict):
        raise _invalid("the event is not a JSON object")
    if event.get("source") != SOURCE or event.get("detail-type") != DETAIL_TYPE:
        return {"action": "ignore", "reason": "not a Step Functions execution status change event"}
    detail = event.get("detail")
    if not isinstance(detail, dict):
        raise _invalid("the event has no detail")
    status = detail.get("status")
    if status != SUCCEEDED:
        return {"action": "ignore", "reason": f"the execution status is {status!r}, only {SUCCEEDED} is processed",
                "status": status}
    from_input, from_output = _document_id(detail, "input"), _document_id(detail, "output")
    if from_input and from_output and from_input != from_output:
        raise _invalid("detail.input and detail.output name different documents")
    document_id = from_input or from_output
    if not document_id:
        raise _invalid("neither detail.input nor detail.output holds a document ID (document.id)")
    if ".." in document_id or not DOCUMENT_ID.match(document_id):
        raise _invalid("the document ID is not a valid document ID (1 to 256 letters, digits, . _ - /)")
    execution = str(detail.get("name") or detail.get("executionArn") or "")[:MAX_EXECUTION]
    return {"action": "process", "document_id": document_id, "execution": execution,
            "event_date": _event_date(event, today)}
