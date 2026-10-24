"""
SyteLine integration Lambda: the ONE function of the deployment (specs/007). What runs is decided by the request:

  * an explicit `action`: health | add_note | get_notes | document_event, and the internal work_upload | work_save | sweep
    (started by this function itself or by the schedule);
  * otherwise an API Gateway request: its route picks the handler (handlers/routing.py), e.g. POST /confirmed-orders;
  * otherwise the default action, `document_event`: the IDP processing-complete event (specs/006). A payload that is not
    such an event is refused; the connectivity check needs {"action": "health"}.

See specs/007-single-function-existing-vpc/contracts/dispatch.md and specs/001-syteline-erp-integration/contracts/.

Credentials come from Secrets Manager (SYTELINE_SECRET_ARN) or environment variables and are never
logged or returned. Writes (add_note) require SYTELINE_LIVE_WRITES.
"""

import json
import logging
import time

from syteline import config as cfgmod
from syteline import errors
from syteline.errors import (ErpError, ErpUnreachable, IntegrationError, SecretStoreUnavailable,
                              failure)
from syteline.health import run_health

log = logging.getLogger()
log.setLevel(logging.INFO)

ACTIONS = ("health", "add_note", "get_notes")
DEFAULT_ACTION = "document_event"  # the IDP processing-complete event carries no `action`: it is what a bare payload means
OK_OUTCOMES = ("ignored", "already_processed", "reserved", "save_accepted")
# the background actions the function starts on itself (async) or the schedule starts: action -> (handlers module, entry)
WORK_ACTIONS = {"work_upload": ("worker", "work_upload"), "work_save": ("worker", "work_save"),
                "sweep": ("sweeper", "sweep")}

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
        soap = _make_client()
    except SecretStoreUnavailable as exc:  # unreachable or unreadable credentials store (FR-013)
        return failure(action, errors.error(errors.SECRET_STORE_UNREACHABLE, str(exc)))
    try:
        if action == "add_note":
            return notes.add_note(soap, event["record_type"], event["record_key"], event["text"],
                                  bool(event.get("internal", True)))
        return notes.get_notes(soap, event["record_type"], event["record_key"], event.get("since"))
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
    from handlers import event_intake

    # `"debug": true` next to the event (or beside `event`) asks for the underlying message of an IDP failure in the response
    debug = isinstance(raw, dict) and raw.get("debug") is True
    out = event_intake.process(payload, context, debug=debug)
    out["action"] = "document_event"
    out["ok"] = out.get("outcome") in OK_OUTCOMES
    return out


def _api(event, context):
    """An API Gateway request (specs/007): the route picks the handler, which keeps its own request and response."""
    from handlers import routing

    module = routing.find_route(event)
    if module is None:
        return {"statusCode": 404, "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"errors": [errors.error(errors.NOT_FOUND, "no such route")]})}
    return routing.load(module).handler(event, routing.Budget(context, routing.BUDGETS[module]))


def _background(action, payload, context):
    from handlers import routing

    module, entry = WORK_ACTIONS[action]
    return getattr(routing.load(module), entry)(payload, context)  # the real remaining time: no budget


def lambda_handler(event, context):
    from handlers import routing
    from syteline import eventlog

    eventlog.log_entry(event, log)  # FR-016: one line with the event (redacted unless LOG_EVENTS=true); never raises
    raw = event
    event = event if isinstance(event, dict) else {}
    explicit = event.get("action")
    if not explicit and routing.is_api_event(event):
        return _api(event, context)
    action = explicit or DEFAULT_ACTION
    ctx = routing.Budget(context, routing.BUDGETS[action]) if action in routing.BUDGETS else context
    start = time.monotonic()
    try:
        if action == "health":
            result = run_health(event, ctx)
        elif action == "document_event":
            result = _document_event(raw, not explicit, ctx)
        elif action in WORK_ACTIONS:
            result = _background(action, event, ctx)
        elif action in ACTIONS:
            result = _note_action(action, event)
        else:
            result = failure(action, errors.error(errors.UNKNOWN_ACTION, f"unknown action: {action}"))
    except IntegrationError as exc:
        result = failure(action, exc.err, **exc.extra)
    log.info(json.dumps({
        "type": action, "ok": result.get("ok"),
        "record": [event.get("record_type"), event.get("record_key")] if action in ("add_note", "get_notes") else None,
        "errors": [e.get("code") for e in result.get("errors", [])],
        "ms": int((time.monotonic() - start) * 1000),
    }, default=str))
    return result


if __name__ == "__main__":  # local run: SYTELINE_* env vars required (dry run: SYTELINE_DRY_RUN=true)
    print(json.dumps(lambda_handler({"action": "health"}, None), indent=2))
