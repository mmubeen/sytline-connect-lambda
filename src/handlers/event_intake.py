"""The IDP state machine's "processing finished" event: read the document's result, and reserve and save when usable.

Started by EventBridge (or invoked directly with the event). Order (specs/006, research R2 to R8):
  parse the event -> read the document ONCE through the IDP SDK -> scan result summary -> usability checks of the
  job-number workflow -> live writes / defaults checks -> once-per-document claim -> reserve (run_reserve) ->
  accept the save and start the worker.
Nothing in the event except the document ID, the execution name and its time is used. The response is the outcome plus
the summary; field values appear only in `summary.fields`, never in the log or the stored record.
"""

import hashlib
import time
import uuid

from syteline import config, errors, eventdoc, eventstore, idp, savestart
from syteline.client import SoapIdoClient
from syteline.errors import IntegrationError, SecretStoreUnavailable
from syteline.reservations import ReservationStore, effective_state, numbers_report
from syteline.reserve import run_reserve

from ._common import log_event

# Overridable factories (tests inject fakes).
_store_factory = None
_events_factory = None
_clients_factory = None  # () -> soap
_lambda_factory = None

UPLOADED_BY = "event-intake"


def _store():
    return _store_factory() if _store_factory else ReservationStore.from_env()


def _events(store):
    return _events_factory() if _events_factory else eventstore.EventStore(store.table)


def _soap():
    if _clients_factory:
        return _clients_factory()
    return SoapIdoClient(config.load_config(), timeout=config.timeout(), verify=config.verify_tls())


def _lambda():
    if _lambda_factory:
        return _lambda_factory()
    import boto3

    return boto3.client("lambda")


def _remaining(context):
    fn = getattr(context, "get_remaining_time_in_millis", None)
    return fn if callable(fn) else (lambda: 10 ** 9)


def _out(outcome, document_id=None, summary=None, **extra):
    body = {"outcome": outcome}
    if document_id:
        body["document_id"] = document_id
    body.update(extra)
    if summary is not None:
        body["summary"] = summary
    return body


def _refused(exc, document_id=None, summary=None):
    return _out("refused", document_id, summary, errors=[exc.err], **exc.extra)


def _numbers(item):
    report = numbers_report(item, int(time.time()))
    return {"reservation_id": item["reservation_id"], "state": effective_state(item, int(time.time())),
            "co_num": report["co_num"], "jobs": [{"line": j["line"], "job": j["job"]} for j in report["jobs"]]}


def _final(item):
    """A reservation that ended without consuming a number (customer refused, ERP unreachable first) may be tried again."""
    return not (item["state"] == "failed" and not item.get("co_num") and not item.get("job_numbers"))


def _request_ref(document_id):
    return f"event-{hashlib.sha256(document_id.encode()).hexdigest()[:16]}-{uuid.uuid4().hex[:12]}"  # a new attempt is a new reservation


def _log(started, outcome, parsed=None, summary=None, **fields):
    counts = {}
    if summary:
        counts = {"fields": len(summary["fields"]), "below_threshold": len(summary["below_threshold"])}
    log_event("event_intake", started, outcome=outcome,
              document_id=(parsed or {}).get("document_id"), execution=(parsed or {}).get("execution"),
              **counts, **fields)


def process(payload, context):
    """Run the intake for one event (the payload of the `document_event` action); returns the outcome object."""
    started = time.monotonic()
    try:
        parsed = eventdoc.parse(payload)
    except IntegrationError as exc:
        _log(started, "refused", code=exc.err["code"])
        return _refused(exc)
    if parsed["action"] == "ignore":
        _log(started, "ignored", reason=parsed["reason"])
        extra = {k: parsed[k] for k in ("received", "expected", "top_level_keys") if k in parsed}
        return _out("ignored", reason=parsed["reason"], **extra)
    document_id = parsed["document_id"]

    try:  # one read of the document serves the summary and the PO details
        read = idp.read_document(document_id)
        summary = idp.scan_summary(read)
    except IntegrationError as exc:
        _log(started, "refused", parsed, code=exc.err["code"])
        return _refused(exc, document_id)

    try:
        doc = idp.po_details(document_id, read=read)
    except IntegrationError as exc:  # not usable (unreviewed low field, missing field, ...): nothing is reserved
        _log(started, "refused", parsed, summary, code=exc.err["code"])
        return _refused(exc, document_id, summary)

    if not config.live_writes_enabled():  # reserving a number is itself a write
        _log(started, "read_only", parsed, summary, code=errors.LIVE_WRITES_DISABLED)
        return _out("read_only", document_id, summary, reason="live ERP writes are disabled; nothing was reserved",
                    errors=[errors.error(errors.LIVE_WRITES_DISABLED, "live ERP writes are disabled")])
    defaults = config.event_defaults(parsed["event_date"])
    if defaults["missing"]:
        exc = IntegrationError(errors.DEFAULTS_NOT_CONFIGURED, "not configured: " + ", ".join(defaults["missing"]),
                               step="defaults")
        _log(started, "refused", parsed, summary, code=exc.err["code"])
        return _refused(exc, document_id, summary)

    try:
        store = _store()
        events = _events(store)
        first = store.reservation_for_document(document_id)
    except Exception:
        _log(started, "refused", parsed, summary, code=errors.STORE_UNAVAILABLE)
        return _refused(IntegrationError(errors.STORE_UNAVAILABLE, "could not reach the reservation store",
                                         step="record"), document_id, summary)
    if first is not None and _final(first):
        _log(started, "already_processed", parsed, summary, reservation_id=first["reservation_id"])
        return _out("already_processed", document_id, summary, first_outcome=_numbers(first))
    try:
        claimed = events.claim(document_id, parsed["execution"])
    except Exception:
        _log(started, "refused", parsed, summary, code=errors.STORE_UNAVAILABLE)
        return _refused(IntegrationError(errors.STORE_UNAVAILABLE, "could not record the claim", step="record"),
                        document_id, summary)
    if not claimed:
        current = events.get(document_id) or {}
        _log(started, "already_processed", parsed, summary)
        return _out("already_processed", document_id, summary,
                    first_outcome={k: current.get(k) for k in ("state", "outcome", "reservation_id") if current.get(k)})
    try:
        soap = _soap()
    except SecretStoreUnavailable as exc:
        _release(events, document_id)
        _log(started, "refused", parsed, summary, code=errors.SECRET_STORE_UNREACHABLE)
        return _refused(IntegrationError(errors.SECRET_STORE_UNREACHABLE, str(exc), step="erp"), document_id, summary)
    except Exception:
        _release(events, document_id)
        _log(started, "refused", parsed, summary, code=errors.ERP_UNREACHABLE)
        return _refused(IntegrationError(errors.ERP_UNREACHABLE, "could not prepare the ERP connection", step="erp"),
                        document_id, summary)

    request = {"request_ref": _request_ref(document_id), "document_id": document_id,
               "uploaded_by": UPLOADED_BY}
    try:
        reserved = run_reserve(request, soap, store, f"event-intake:{parsed['execution']}", _remaining(context),
                               source=lambda _id: doc, cfg=getattr(soap, "_cfg", None))
    except Exception as exc:  # nothing could be recorded, so nothing was reserved
        _release(events, document_id)
        _log(started, "refused", parsed, summary, code=errors.STORE_UNAVAILABLE, error=type(exc).__name__)
        return _refused(IntegrationError(errors.STORE_UNAVAILABLE, "could not record the reservation", step="record"),
                        document_id, summary)
    item = reserved["reservation"]
    rid = item["reservation_id"]
    if reserved.get("duplicate"):  # cannot happen with a fresh request_ref; never act on someone else's reservation
        _log(started, "already_processed", parsed, summary, reservation_id=rid)
        return _out("already_processed", document_id, summary, first_outcome=_numbers(item))

    failure = reserved.get("failure")
    if failure:
        view = _numbers(item)
        err = errors.error(failure["code"], failure["message"], step=failure["step"])
        if reserved.get("consumed_unused"):  # a number is gone: this document is not tried again automatically
            events.record(document_id, f"failed:{failure['code']}", rid, state="finished")
            _log(started, "failed", parsed, summary, code=failure["code"], reservation_id=rid)
            return _out("failed", document_id, summary, errors=[err], consumed_unused=reserved["consumed_unused"], **view)
        _release(events, document_id)  # no number was consumed: a later delivery may try again
        _log(started, "read_only", parsed, summary, code=failure["code"], reservation_id=rid)
        extra = {"candidates": reserved["candidates"]} if reserved.get("candidates") else {}
        return _out("read_only", document_id, summary, reason=failure["message"], errors=[err], **extra)

    events.record(document_id, "reserved", rid, state="reserved")
    params = {"document_id": document_id, "order_date": defaults["order_date"],
              "received_date": defaults["received_date"], "warehouse": defaults["warehouse"],
              "order_type": defaults["order_type"]}
    try:
        accepted = savestart.accept_and_start(store, _lambda(), rid, params)
    except Exception:  # the reservation is still `reserved` and can be saved through the API
        accepted = False
    if not accepted:
        _log(started, "reserved", parsed, summary, reservation_id=rid)
        return _out("reserved", document_id, summary, **_numbers(store.get(rid) or item))
    events.record(document_id, "save_accepted", rid, state="save_accepted")
    _log(started, "save_accepted", parsed, summary, reservation_id=rid)
    return _out("save_accepted", document_id, summary, **_numbers(store.get(rid) or item))


def _release(events, document_id):
    try:
        events.release(document_id)
    except Exception:
        pass
