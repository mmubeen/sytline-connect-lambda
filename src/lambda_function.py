"""
SyteLine integration Lambda: direct-invoke actions.

Event: {"action": "health" | "add_note" | "get_notes" | "document_event", ...}; `action` defaults to
"document_event" (the IDP processing-complete event, specs/006); the connectivity check, including the dry run,
needs {"action": "health"}. Order upload is NOT here: it is the private API
(handlers/intake.py, status.py) plus the worker. See
specs/001-syteline-erp-integration/contracts/lambda-actions.md and contracts/api.md.

Credentials come from Secrets Manager (SYTELINE_SECRET_ARN) or environment variables and are never
logged or returned. Writes (add_note) require SYTELINE_LIVE_WRITES.
"""

import json
import logging
import time

from syteline import config as cfgmod
from syteline import errors
from syteline.errors import (ErpError, ErpUnreachable, InsecureTransport, IntegrationError, SecretStoreUnavailable,
                              failure)
from syteline.health import run_health

log = logging.getLogger()
log.setLevel(logging.INFO)

ACTIONS = ("health", "add_note", "get_notes")
DEFAULT_ACTION = "document_event"  # the IDP processing-complete event carries no `action`: it is what a bare payload means
OK_OUTCOMES = ("ignored", "already_processed", "reserved", "save_accepted")

# Overridable factory (tests inject a fake web-service client).
_client_factory = None


def _make_client():
    if _client_factory:
        return _client_factory()
    from syteline.client import SoapIdoClient

    return SoapIdoClient(cfgmod.load_config(), timeout=cfgmod.timeout(), verify=cfgmod.verify_tls())


def _note_action(action, event):
    from syteline import notes

    problems = notes.validate_note_request(event, need_text=(action == "add_note"))
    if problems:
        return failure(action, *problems)
    try:
        if cfgmod.require_https():  # stand-alone deployment: check the (possibly secret-supplied) address first
            problem = cfgmod.transport_problem(cfgmod.load_config()["SYTELINE_WSDL"])
            if problem:
                return failure(action, errors.error(errors.INSECURE_TRANSPORT, problem))
        soap = _make_client()
    except SecretStoreUnavailable as exc:  # unreachable or unreadable credentials store (FR-013)
        return failure(action, errors.error(errors.SECRET_STORE_UNREACHABLE, str(exc)))
    try:
        if action == "add_note":
            return notes.add_note(soap, event["record_type"], event["record_key"], event["text"],
                                  bool(event.get("internal", True)))
        return notes.get_notes(soap, event["record_type"], event["record_key"], event.get("since"))
    except InsecureTransport as exc:  # e.g. the WSDL declares an http:// service address
        return failure(action, errors.error(errors.INSECURE_TRANSPORT, str(exc)))
    except ErpUnreachable as exc:
        return failure(action, errors.error(errors.ERP_UNREACHABLE, str(exc)))
    except ErpError as exc:
        return failure(action, errors.error(errors.ERP_REJECTED, str(exc)))


def _document_event(raw, defaulted, context):
    """The event intake (specs/006). `raw` is the IDP event, or an object holding it under `event`."""
    from syteline import eventdoc

    payload = eventdoc.unwrap(raw)
    if defaulted and not eventdoc.is_status_change(payload):
        return failure("document_event", errors.error(
            errors.EVENT_INVALID, 'no action was given and the payload is not an IDP processing event; to run the '
            'health check pass {"action": "health"}'))
    if not cfgmod.jobs_table():  # the stand-alone deployment has no reservation store and no document source
        return failure("document_event", errors.error(
            errors.ACTION_UNAVAILABLE, "the document_event action is not available in this deployment"))
    from handlers import event_intake

    out = event_intake.process(payload, context)
    out["action"] = "document_event"
    out["ok"] = out.get("outcome") in OK_OUTCOMES
    return out


def lambda_handler(event, context):
    raw = event
    event = event if isinstance(event, dict) else {}
    explicit = event.get("action")
    action = explicit or DEFAULT_ACTION
    start = time.monotonic()
    try:
        if action == "health":
            result = run_health(event, context)
        elif action == "document_event":
            result = _document_event(raw, not explicit, context)
        elif action in ACTIONS:
            result = _note_action(action, event)
        else:
            result = failure(action, errors.error(errors.UNKNOWN_ACTION, f"unknown action: {action}"))
    except IntegrationError as exc:
        result = failure(action, exc.err, **exc.extra)
    log.info(json.dumps({
        "type": action, "ok": result.get("ok"),
        "record": [event.get("record_type"), event.get("record_key")] if action != "health" else None,
        "errors": [e.get("code") for e in result.get("errors", [])],
        "ms": int((time.monotonic() - start) * 1000),
    }, default=str))
    return result


if __name__ == "__main__":  # local run: SYTELINE_* env vars required (dry run: SYTELINE_DRY_RUN=true)
    print(json.dumps(lambda_handler({"action": "health"}, None), indent=2))
