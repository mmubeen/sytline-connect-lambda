"""
SyteLine integration Lambda: direct-invoke actions.

Event: {"action": "health" | "add_note" | "get_notes", ...}; `action` defaults to "health" (the
original connectivity check, including the dry run). Order upload is NOT here: it is the private API
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
from syteline.errors import ErpError, ErpUnreachable, IntegrationError, failure
from syteline.health import run_health

log = logging.getLogger()
log.setLevel(logging.INFO)

ACTIONS = ("health", "add_note", "get_notes")

# Overridable factory (tests inject a fake SQL client).
_sql_factory = None


def _make_sql():
    if _sql_factory:
        return _sql_factory()
    from syteline.sql import SqlClient

    return SqlClient(cfgmod.load_config())


def _note_action(action, event):
    from syteline import notes

    problems = notes.validate_note_request(event, need_text=(action == "add_note"))
    if problems:
        return failure(action, *problems)
    sql = _make_sql()
    try:
        if action == "add_note":
            return notes.add_note(sql, event["record_type"], event["record_key"], event["text"],
                                  bool(event.get("internal", True)))
        return notes.get_notes(sql, event["record_type"], event["record_key"], event.get("since"))
    except ErpUnreachable as exc:
        return failure(action, errors.error(errors.ERP_UNREACHABLE, str(exc)))
    except ErpError as exc:
        return failure(action, errors.error(errors.ERP_REJECTED, str(exc)))
    finally:
        if hasattr(sql, "close"):
            sql.close()


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    action = event.get("action") or "health"
    start = time.monotonic()
    try:
        if action == "health":
            result = run_health(event, context)
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
    print(json.dumps(lambda_handler({}, None), indent=2))
