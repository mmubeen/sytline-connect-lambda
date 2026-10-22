"""
The upload sequence (instruction sheet section 4, adapted to FR-018: web service only; see
research.md R5/R6):

  resolve customer (read) -> [live-writes gate] -> NextCoSp (method call) -> insert SLCos
  -> per line: NextWOSp (method call) -> insert CCT_Coitems -> insert CCTJobs -> read back and verify.

Outcome classification (FR-004):
  failed                 - nothing can have been written (or the ERP rejected the very first row and
                           a successful read-back proves it is absent). Retriable.
  needs_reconciliation   - any row may exist and the outcome is not proven: timeouts after a request
                           was sent, rejections after earlier rows were written, read-back failure or
                           mismatch. No delete, no retry, ever.
  succeeded              - every row verified by read-back.

ASSUMPTIONS (confirm with discovery task T013): the web-service names in idos.py, the property names
written to SLCos / CCT_Coitems / CCTJobs and the way the teardown value is stored (here: property
`Teardown` on CCT_Coitems). Verified only against fakes.
"""

import logging

from . import config, defaults, errors, idos, numbers, resolver
from .errors import (ErpError, ErpRejected, ErpUncertain, ErpUnreachable, IntegrationError, LiveWritesDisabled)
from .validation import today

log = logging.getLogger(__name__)

MIN_REMAINING_MS = 5000
CO_IDO, LINE_IDO, JOB_IDO = idos.CO_IDO, idos.LINE_IDO, idos.JOB_IDO


def _q(v):
    return "'" + str(v).replace("'", "''") + "'"


class _Stop(Exception):
    """Internal: carries a finished outcome."""

    def __init__(self, outcome):
        self.outcome = outcome


class Upload:
    def __init__(self, request, soap, cfg, remaining_ms=None, on_progress=None):
        self.req, self.soap, self.cfg = request, soap, cfg
        self.remaining_ms = remaining_ms or (lambda: 10 ** 9)
        self.on_progress = on_progress
        self.possibly = []  # [{"kind","key"}]
        self.customer = None
        self.co_num = None
        self.lines = []  # [{"line","job"}]
        self.numbers = numbers.NumberSource(soap, cfg)

    # -- outcome builders ----------------------------------------------------------------
    def _failed(self, code, message, step):
        return {"state": "failed", "code": code, "message": message, "step": step,
                "customer_match": self.customer or {}}

    def _park(self, code, message, step):
        return {"state": "needs_reconciliation", "code": code, "message": message, "step": step,
                "possibly_written": list(self.possibly), "customer_match": self.customer or {}}

    def _mark(self, kind, key):
        self.possibly.append({"kind": kind, "key": key})
        if self.on_progress:
            self.on_progress(list(self.possibly), self.customer)

    def _time(self, step):
        if self.remaining_ms() < MIN_REMAINING_MS:
            msg = "not enough Lambda time left to continue safely"
            raise _Stop(self._park(errors.TIME_BUDGET_EXHAUSTED, msg, step) if self.possibly
                        else self._failed(errors.TIME_BUDGET_EXHAUSTED, msg, step))

    # -- steps ---------------------------------------------------------------------------
    def _allocate(self, operation, kind, step):
        """Reserve a CO/job number ("next_co" / "next_job") through the operation's route: the web service, or the
        database when no endpoint is configured (syteline.routes, numbers.NumberSource)."""
        self._time(step)
        out_fn = self._park if self.possibly else self._failed  # a number may be consumed, but no row exists unless earlier rows were written
        try:
            key = self.numbers.next(operation)
        except LiveWritesDisabled:
            raise _Stop(self._failed(errors.LIVE_WRITES_DISABLED, "live ERP writes are disabled", step))
        except IntegrationError as exc:  # DATABASE_NOT_CONFIGURED: refused before any connection
            raise _Stop(out_fn(exc.err["code"], exc.err["message"], step))
        except ErpUncertain as exc:  # the number may or may not be consumed; never retried
            raise _Stop(out_fn(errors.UNCERTAIN_OUTCOME, f"{kind} number allocation, outcome unknown: {exc}", step))
        except ErpError as exc:
            code = errors.ERP_REJECTED if isinstance(exc, ErpRejected) else errors.ERP_UNREACHABLE
            raise _Stop(out_fn(code, f"{kind} number allocation failed: {exc}", step))
        if not key:
            raise _Stop(out_fn(errors.ERP_REJECTED, f"{kind} number allocation returned no number", step))
        return key

    def _insert(self, ido, values, kind, key, step):
        """Insert one row. The row is recorded as possibly written BEFORE the request is sent."""
        self._time(step)
        first = not self.possibly
        self._mark(kind, key)
        try:
            return self.soap.insert(ido, values)
        except LiveWritesDisabled:
            self.possibly.pop()
            raise _Stop(self._failed(errors.LIVE_WRITES_DISABLED, "live ERP writes are disabled", step))
        except ErpUnreachable as exc:  # failed while connecting: this request cannot have been processed
            if first:
                self.possibly.pop()
                raise _Stop(self._failed(errors.ERP_UNREACHABLE, f"{step}: {exc}", step))
            self.possibly.pop()
            raise _Stop(self._park(errors.ERP_UNREACHABLE, f"{step}: {exc}", step))
        except ErpRejected as exc:
            if first and self._proven_absent(ido, key):
                self.possibly.pop()
                raise _Stop(self._failed(errors.ERP_REJECTED, f"{step}: {exc}", step))
            raise _Stop(self._park(errors.ERP_REJECTED, f"{step}: {exc}", step))
        except ErpUncertain as exc:  # timeout etc.: it may have been processed. Never classify as failed.
            raise _Stop(self._park(errors.UNCERTAIN_OUTCOME, f"{step}: {exc}", step))

    def _proven_absent(self, ido, key):
        try:
            return not self.soap.read_back(ido, ["CoNum"], f"CoNum = {_q(key)}")
        except Exception:
            return False

    def _verify(self):
        step = "read_back"
        try:
            cos = self.soap.read_back(CO_IDO, ["CoNum", "CustNum"], f"CoNum = {_q(self.co_num)}")
            lines = self.soap.read_back(LINE_IDO, ["CoNum", "CoLine", "Item", "QtyOrdered"],
                                        f"CoNum = {_q(self.co_num)}")
            jobs = [self.soap.read_back(JOB_IDO, ["Job", "CoNum", "CoLine"], f"Job = {_q(j['job'])}")
                    for j in self.lines]
        except Exception as exc:
            raise _Stop(self._park(errors.UNCERTAIN_OUTCOME, f"read-back failed: {exc}", step))
        problems = []
        if len(cos) != 1 or str(cos[0].get("CoNum", "")).strip() != self.co_num:
            problems.append("CO header not found or duplicated")
        elif str(cos[0].get("CustNum", "")).strip() != self.customer["cust_num"]:
            problems.append("CO customer differs from the resolved customer")
        by_line = {str(r.get("CoLine")): r for r in lines}
        for i, ln in enumerate(self.req["lines"], start=1):
            row = by_line.get(str(i))
            if row is None:
                problems.append(f"line {i} missing")
                continue
            if str(row.get("Item", "")).strip() != ln["item"].strip():
                problems.append(f"line {i} item differs")
            try:
                if float(row.get("QtyOrdered")) != float(ln["quantity"]):
                    problems.append(f"line {i} quantity differs")
            except (TypeError, ValueError):
                problems.append(f"line {i} quantity unreadable")
        for entry, found in zip(self.lines, jobs):
            if len(found) != 1:
                problems.append(f"job {entry['job']} (line {entry['line']}) not found")
        if problems:
            raise _Stop(self._park(errors.READBACK_MISMATCH, "; ".join(problems), step))

    # -- main ----------------------------------------------------------------------------
    def run(self):
        req = self.req
        try:
            step = "resolve_customer"
            try:
                self.customer = resolver.resolve_customer(self.soap, req["customer"], self.cfg)
            except IntegrationError as exc:
                raise _Stop(self._failed(exc.err["code"], exc.err["message"], step))
            except ErpError as exc:
                raise _Stop(self._failed(errors.ERP_UNREACHABLE, str(exc), step))

            if not config.live_writes_enabled():  # allocating numbers is a write too (FR-016)
                raise _Stop(self._failed(errors.LIVE_WRITES_DISABLED,
                                         "live ERP writes are disabled; nothing was written", "live_writes_check"))

            found, warning = defaults.read(self.customer, self.soap, self.cfg)  # never blocks the upload
            if warning:
                log.warning("%s", warning)
            header = {"CoNum": None, "CustNum": self.customer["cust_num"], "CustSeq": self.customer["cust_seq"],
                      "OrderDate": req.get("order_date") or today(), "Stat": "O"}
            for key in ("terms_code", "ship_code"):
                if found and found[key]:
                    header[idos.CO_HEADER_PROPS[key]] = found[key]
            self.co_num = self._allocate("next_co", "CO", "number_co")
            header["CoNum"] = self.co_num
            self._insert(CO_IDO, header, "co", self.co_num, "create_co")

            whse = req.get("warehouse") or self.cfg.get("SYTELINE_DEFAULT_WHSE", "")
            ordtype = req.get("order_type") or self.cfg.get("SYTELINE_DEFAULT_ORDTYPE", "")
            for i, ln in enumerate(req["lines"], start=1):
                job = self._allocate("next_job", "job", f"number_job_{i}")
                vals = {"CoNum": self.co_num, "CoLine": i, "Item": ln["item"], "UM": ln["uom"],
                        "QtyOrdered": ln["quantity"]}
                for src, dst in (("due_date", "DueDate"), ("price", "Price"), ("teardown", "Teardown")):
                    if src in ln:
                        vals[dst] = ln[src]
                self._insert(LINE_IDO, vals, "line", f"{self.co_num}/{i}", f"create_line_{i}")
                self._insert(JOB_IDO, {
                    "Job": job, "OrdType": ordtype, "Whse": whse, "Item": ln["item"],
                    "QtyReleased": ln["quantity"], "CoNum": self.co_num, "CoLine": i,
                }, "job", job, f"create_job_{i}")
                self.lines.append({"line": i, "job": job})

            self._verify()
            return {"state": "succeeded", "co_num": self.co_num, "lines": self.lines,
                    "customer_match": self.customer}
        except _Stop as stop:
            return stop.outcome
        finally:
            self.numbers.close()  # the database connection (if the numbers came from the database) ends with the upload


def run_upload(request, soap, cfg, remaining_ms=None, on_progress=None):
    return Upload(request, soap, cfg, remaining_ms, on_progress).run()
