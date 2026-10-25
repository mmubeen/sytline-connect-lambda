"""Stage 1 of the job-number workflow: reserve one CO number and one job number per PO line.

Order (research.md R5, R12): read the IDP document (the PO details) -> record the reservation -> resolve the customer (read) -> live-writes gate ->
NextCoSp -> NextWOSp per line (each number recorded as soon as it is obtained). Nothing is ever inserted:
a CO header, line or job row is written only by the save stage. A number obtained is consumed even if a later
step fails (SyteLine's own behavior), so failures report the numbers already obtained as consumed and unused.
"""

import logging

from . import config, errors, idp, numbers, preflight, resolver, validation
from .errors import ErpError, ErpRejected, ErpUncertain, IntegrationError, LiveWritesDisabled
from .pipeline import MIN_REMAINING_MS

log = logging.getLogger(__name__)


class _Stop(Exception):
    def __init__(self, code, message, step, extra=None):
        self.code, self.message, self.step, self.extra = code, message, step, extra or {}


def run_reserve(request, soap, store, caller_identity, remaining_ms=None, source=None, cfg=None):
    """Return {"duplicate", "reservation", "failure"?, "consumed_unused"?}.

    The PO details come from the IDP document `request["document_id"]` (`source`, default `idp.po_details`), never
    from the caller. Raises IntegrationError when the document cannot be used (nothing is recorded or reserved),
    jobs.RefConflict (same request_ref, different request) and whatever the store raises when the reservation
    cannot be recorded at all (nothing was reserved in that case).
    """
    remaining_ms = remaining_ms or (lambda: 10 ** 9)
    doc = (source or idp.po_details)(request["document_id"])
    details = doc["details"]
    stored = {"request_ref": request["request_ref"], "document_id": request["document_id"],
              "line_count": len(details["lines"])}
    reservation, duplicate = store.create_reservation(
        stored, validation.payload_hash(request), doc["fingerprint"], request["uploaded_by"], caller_identity)
    if duplicate:
        return {"duplicate": True, "reservation": reservation}
    rid = reservation["reservation_id"]
    consumed = []  # numbers obtained so far, kept in memory too in case the store fails

    source_numbers = numbers.NumberSource(soap, cfg)  # one database connection (if any) for the whole reservation

    def allocate(operation, kind, step):
        if remaining_ms() < MIN_REMAINING_MS:
            raise _Stop(errors.TIME_BUDGET_EXHAUSTED, "not enough Lambda time left to continue safely", step)
        try:
            number = source_numbers.next(operation)
        except LiveWritesDisabled:
            raise _Stop(errors.LIVE_WRITES_DISABLED, "live ERP writes are disabled", step)
        except IntegrationError as exc:  # DATABASE_NOT_CONFIGURED: refused before any connection
            raise _Stop(exc.err["code"], exc.err["message"], step)
        except ErpUncertain as exc:  # a number may be consumed; never retried
            raise _Stop(errors.UNCERTAIN_OUTCOME, f"{kind} number allocation, outcome unknown: {exc}", step)
        except ErpError as exc:
            code = errors.ERP_REJECTED if isinstance(exc, ErpRejected) else errors.ERP_UNREACHABLE
            raise _Stop(code, f"{kind} number allocation failed: {exc}", step)
        if not number:
            raise _Stop(errors.ERP_REJECTED, f"{kind} number allocation returned no number", step)
        return number

    def record(fn, *args):
        try:
            fn(*args)
        except Exception as exc:  # the number is consumed but could not be written down
            raise _Stop(errors.STORE_UNAVAILABLE, f"could not record the reserved number: {type(exc).__name__}",
                        "record_number")

    try:
        step = "resolve_customer"
        try:
            customer = resolver.resolve_customer(soap, details["customer"], cfg)
        except IntegrationError as exc:  # includes the candidates of a refused customer match
            raise _Stop(exc.err["code"], exc.err["message"], step, exc.extra)
        except ErpError as exc:
            raise _Stop(errors.ERP_UNREACHABLE, str(exc), step)
        record(store.set_customer, rid, customer)

        if not config.live_writes_enabled():  # reserving a number is itself a write (FR-014)
            raise _Stop(errors.LIVE_WRITES_DISABLED, "live ERP writes are disabled; no number was reserved",
                        "live_writes_check")

        if preflight.enabled():  # no number is consumed for a save the ERP would refuse halfway
            try:
                missing = preflight.check(soap, details["lines"])
            except ErpError as exc:
                raise _Stop(errors.ERP_UNREACHABLE, f"could not check the ERP's property names: {exc}", "check_properties")
            if missing:
                raise _Stop(errors.ERP_PROPERTIES_UNKNOWN, preflight.message(missing), "check_properties", {"missing": missing})

        co_num = config.fixed_co_number()  # CO_NUMBER (a test setting): a number already consumed, reused, none allocated
        if not co_num:
            co_num = allocate("next_co", "CO", "number_co")
            consumed.append({"kind": "co", "number": co_num})
        record(store.set_co_num, rid, co_num)
        jobs = []
        for i in range(1, len(details["lines"]) + 1):
            fixed = config.fixed_job_number()  # JOB_NUMBER (a test setting) wins over the document's own number
            job = fixed or details["lines"][i - 1].get("job")  # a job number given is used, not allocated
            if job:
                source = "configured" if fixed else "document"
            else:
                job, source = allocate("next_job", "job", f"number_job_{i}"), "allocated"
                consumed.append({"kind": "job", "line": i, "number": job})
            jobs.append({"line": i, "job": job, "source": source})
            record(store.add_job_number, rid, list(jobs))
        record(store.mark_reserved, rid)
        return {"duplicate": False, "reservation": store.get(rid)}
    except _Stop as stop:
        try:
            store.mark_failed(rid, stop.code, stop.message, stop.step)
        except Exception:
            log.exception("could not mark reservation %s failed", rid)
        out = {"duplicate": False, "reservation": store.get(rid) or reservation,
               "failure": {"code": stop.code, "message": stop.message, "step": stop.step}}
        if consumed:
            out["consumed_unused"] = consumed
        if stop.extra.get("candidates"):
            out["candidates"] = stop.extra["candidates"]
        if stop.extra.get("missing"):
            out["missing_properties"] = stop.extra["missing"]
        return out
    finally:
        source_numbers.close()
