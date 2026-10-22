"""POST /job-reservations: reserve one CO number and one job number per line (writes no order, line or job)."""

import json
import time

from syteline import config, errors, validation
from syteline.client import SoapIdoClient
from syteline.errors import SecretStoreUnavailable
from syteline.jobs import RefConflict
from syteline.reservations import ReservationStore, effective_state, numbers_report
from syteline.reserve import run_reserve

from ._common import caller_identity, log_event, response

# Overridable factories (tests inject fakes).
_store_factory = None
_clients_factory = None  # () -> soap

UNPROCESSABLE = (errors.CUSTOMER_NOT_FOUND, errors.CUSTOMER_AMBIGUOUS, errors.LIVE_WRITES_DISABLED)


def _store():
    return _store_factory() if _store_factory else ReservationStore.from_env()


def _soap():
    if _clients_factory:
        return _clients_factory()
    return SoapIdoClient(config.load_config(), timeout=config.timeout(), verify=config.verify_tls())


def _remaining(context):
    fn = getattr(context, "get_remaining_time_in_millis", None)
    return fn if callable(fn) else (lambda: 10 ** 9)


def _body(item, duplicate):
    now = int(time.time())
    report = numbers_report(item, now)
    out = {"reservation_id": item["reservation_id"], "state": effective_state(item, now), "duplicate": duplicate,
           "co_num": report["co_num"], "jobs": [{"line": j["line"], "job": j["job"]} for j in report["jobs"]]}
    if item.get("customer_match"):
        out["customer_match"] = {k: item["customer_match"][k] for k in ("cust_num", "cust_seq")
                                 if k in item["customer_match"]}
    if out["state"] == "failed":
        out["failure"] = item.get("failure") or {"code": errors.UNCERTAIN_OUTCOME,
                                                 "message": "the reservation never finished", "step": "reserve"}
        consumed = [{"kind": "job", "line": j["line"], "number": j["job"]} for j in report["jobs"]]
        if report["co_num"]:
            consumed.insert(0, {"kind": "co", "number": report["co_num"]})
        if consumed:
            out["consumed_unused"] = consumed
    return out


def handler(event, context):
    started = time.monotonic()
    try:
        body = json.loads(event.get("body") or "")
    except (TypeError, ValueError):
        log_event("reserve", started, outcome="400")
        return response(400, {"errors": [errors.error(errors.VALIDATION, "body must be valid JSON")]})

    problems = validation.validate_reserve(body)
    if problems:
        log_event("reserve", started, outcome="400",
                  request_ref=body.get("request_ref") if isinstance(body, dict) else None)
        return response(400, {"errors": problems})

    caller = caller_identity(event)
    try:
        store = _store()
        soap = _soap()
    except SecretStoreUnavailable as exc:
        log_event("reserve", started, outcome="502", request_ref=body["request_ref"])
        return response(502, {"errors": [errors.error(errors.SECRET_STORE_UNREACHABLE, str(exc))]})
    except Exception:
        log_event("reserve", started, outcome="503", request_ref=body["request_ref"])
        return response(503, {"errors": [errors.error(errors.STORE_UNAVAILABLE, "could not reach the reservation store")]})

    try:
        out = run_reserve(body, soap, store, caller, _remaining(context))
    except RefConflict:
        log_event("reserve", started, outcome="409", request_ref=body["request_ref"])
        return response(409, {"errors": [errors.error(
            errors.REQUEST_REF_CONFLICT, "request_ref was already used with a different request")]})
    except Exception:
        # nothing was recorded, so nothing was reserved: the caller can retry
        log_event("reserve", started, outcome="503", request_ref=body["request_ref"])
        return response(503, {"errors": [errors.error(errors.STORE_UNAVAILABLE, "could not record the reservation")]})

    item = out["reservation"]
    fields = dict(request_ref=body["request_ref"], reservation_id=item["reservation_id"],
                  uploaded_by=body["uploaded_by"], caller=caller, co_num=item.get("co_num"))
    if out["duplicate"]:
        log_event("reserve", started, outcome="200", duplicate=True, **fields)
        return response(200, _body(item, True))
    failure = out.get("failure")
    if failure:
        status = 422 if failure["code"] in UNPROCESSABLE else 503 if failure["code"] == errors.STORE_UNAVAILABLE else 502
        view = _body(item, False)
        view["errors"] = [errors.error(failure["code"], failure["message"], step=failure["step"])]
        if out.get("consumed_unused"):
            view["consumed_unused"] = out["consumed_unused"]
        log_event("reserve", started, outcome=str(status), code=failure["code"], step=failure["step"], **fields)
        return response(status, view)
    log_event("reserve", started, outcome="201", **fields)
    return response(201, _body(item, False))
