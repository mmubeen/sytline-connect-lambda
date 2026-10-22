"""Stage 2 of the job-number workflow: save the order information using the numbers reserved in stage 1.

Follows the PO OCR demo's per-line order (runner._process_line) on top of the existing upload bookkeeping:

  CO header -> per line: CO line carrying its job number (ref_num) -> teardown date (separate update) -> job
  -> read back every saved line, job and the link between them.

It never obtains a number (the reservation already holds them). Safety rules are those of the upload (FR-004 of
feature 001): stop at the first problem, record a row as possibly written BEFORE sending it, never delete,
never retry. Outcome states: saved | partly_saved | failed | needs_reconciliation (research.md R7, R8).
"""

from . import config, errors, idos
from .errors import ErpError, ErpRejected, LiveWritesDisabled
from .pipeline import CO_IDO, JOB_IDO, LINE_IDO, Upload, _q, _Stop
from .validation import today

SAVED, FAILED, NOT_ATTEMPTED, UNVERIFIED = "saved", "failed", "not_attempted", "unverified"


class SaveRun(Upload):
    def __init__(self, reservation, request, soap, cfg, remaining_ms=None, on_progress=None):
        super().__init__(request, soap, cfg, remaining_ms, on_progress)
        self.reservation = reservation
        self.customer = reservation.get("customer_match") or {}
        self.co_num = str(reservation["co_num"])
        self.jobs = [{"line": int(j["line"]), "job": str(j["job"])} for j in reservation["job_numbers"]]
        self.results = [{"line": j["line"], "job": j["job"], "item": ln["item"], "status": NOT_ATTEMPTED,
                         "warnings": []} for j, ln in zip(self.jobs, request["lines"])]
        self._written = set()  # lines whose line row and job row were both accepted (not yet read back)

    # -- bookkeeping -----------------------------------------------------------------------
    def _mark(self, kind, key):
        self.possibly.append({"kind": kind, "key": key})
        if self.on_progress:
            self.on_progress(list(self.possibly), [dict(r) for r in self.results])

    def _outcome(self, base):
        return dict(base, results=[dict(r) for r in self.results], co_num=self.co_num)

    # -- steps -----------------------------------------------------------------------------
    def _teardown(self, i, value):
        """Set the line's teardown date (a separate update, as in the demo). A definite rejection is a warning;
        anything else leaves the outcome unknown and stops the run."""
        step = f"teardown_{i}"
        self._time(step)
        try:
            self.soap.update(LINE_IDO, {"CoNum": self.co_num, "CoLine": i}, {idos.LINE_TEARDOWN_PROP: value})
        except ErpRejected as exc:
            self.results[i - 1]["warnings"].append(f"teardown date not set: {exc}")
        except LiveWritesDisabled:
            raise _Stop(self._park(errors.LIVE_WRITES_DISABLED, "live ERP writes are disabled", step))
        except ErpError as exc:
            raise _Stop(self._park(errors.UNCERTAIN_OUTCOME, f"{step}: {exc}", step))

    def _read_back(self, lines):
        """{line: problem or None} for the given line numbers, plus a header problem; None if the read failed."""
        try:
            cos = self.soap.read_back(CO_IDO, ["CoNum", "CustNum"], f"CoNum = {_q(self.co_num)}")
            rows = self.soap.read_back(LINE_IDO, ["CoNum", "CoLine", "Item", "QtyOrdered", idos.LINK_NUM_PROP],
                                       f"CoNum = {_q(self.co_num)}")
            found = {j["job"]: self.soap.read_back(JOB_IDO, ["Job", "CoNum", "CoLine"], f"Job = {_q(j['job'])}")
                     for j in self.jobs if j["line"] in lines}
        except Exception:
            return None
        header = None
        if len(cos) != 1 or str(cos[0].get("CoNum", "")).strip() != self.co_num:
            header = "CO header not found or duplicated"
        elif str(cos[0].get("CustNum", "")).strip() != str(self.customer.get("cust_num", "")):
            header = "CO customer differs from the resolved customer"
        by_line = {str(r.get("CoLine")): r for r in rows}
        problems = {}
        for j in self.jobs:
            i = j["line"]
            if i not in lines:
                continue
            ln = self.req["lines"][i - 1]
            row, p = by_line.get(str(i)), []
            if row is None:
                p.append("CO line missing")
            else:
                if str(row.get("Item", "")).strip() != ln["item"].strip():
                    p.append("item differs")
                try:
                    if float(row.get("QtyOrdered")) != float(ln["quantity"]):
                        p.append("quantity differs")
                except (TypeError, ValueError):
                    p.append("quantity unreadable")
                if str(row.get(idos.LINK_NUM_PROP, "")).strip() != j["job"]:
                    p.append("CO line does not reference its job number")
            job_rows = found[j["job"]]
            if len(job_rows) != 1:
                p.append("job not found or duplicated")
            elif (str(job_rows[0].get("CoNum", "")).strip() != self.co_num
                  or str(job_rows[0].get("CoLine", "")).strip() != str(i)):
                p.append("job is not linked back to its CO line")
            problems[i] = "; ".join(p) or None
        return {"header": header, "lines": problems}

    def _apply_read_back(self, lines):
        """Read back `lines`; mark each saved or unverified. Returns True only if everything was confirmed."""
        checked = self._read_back(set(lines))
        for i in lines:
            self.results[i - 1]["status"] = UNVERIFIED
        if checked is None:
            return False
        ok = checked["header"] is None
        for i in lines:
            problem = checked["lines"].get(i)
            if problem is None:
                self.results[i - 1]["status"] = SAVED
            else:
                self.results[i - 1]["warnings"].append(f"read-back: {problem}")
                ok = False
        return ok and all(checked["lines"].get(i) is None for i in lines)

    # -- main ------------------------------------------------------------------------------
    def run(self):
        req = self.req
        try:
            if not config.live_writes_enabled():
                raise _Stop(self._failed(errors.LIVE_WRITES_DISABLED,
                                         "live ERP writes are disabled; nothing was written", "live_writes_check"))
            header = {"CoNum": self.co_num, "CustNum": self.customer["cust_num"], "CustSeq": self.customer["cust_seq"],
                      "OrderDate": req.get("order_date") or today(), "Stat": "O",
                      idos.CO_HEADER_PROPS["cust_po"]: req["po_number"]}
            if req.get("received_date"):
                header[idos.CO_HEADER_PROPS["received_date"]] = req["received_date"]
            self._insert(CO_IDO, header, "co", self.co_num, "create_co")

            whse = req.get("warehouse") or self.cfg.get("SYTELINE_DEFAULT_WHSE", "")
            ordtype = req.get("order_type") or self.cfg.get("SYTELINE_DEFAULT_ORDTYPE", "")
            link = idos.LINE_LINK_PROPS
            for entry, ln in zip(self.jobs, req["lines"]):
                i, job = entry["line"], entry["job"]
                vals = {"CoNum": self.co_num, "CoLine": i, "Item": ln["item"], "UM": ln["uom"],
                        "QtyOrdered": ln["quantity"], link["ref_type"]: "J", link["ref_num"]: job,
                        link["ref_line_suf"]: 0, link["ref_release"]: 0}
                for src, dst in (("due_date", "DueDate"), ("price", "Price")):
                    if src in ln:
                        vals[dst] = ln[src]
                self._insert(LINE_IDO, vals, "line", f"{self.co_num}/{i}", f"create_line_{i}")
                if "teardown" in ln:
                    self._teardown(i, ln["teardown"])
                jvals = {"Job": job, "OrdType": ordtype, "Whse": whse, "Item": ln["item"],
                         "QtyReleased": ln["quantity"], "CoNum": self.co_num, "CoLine": i,
                         idos.JOB_EXTRA_PROPS["root_job"]: job}
                for src in ("engine_sn", "serial_num", "packing"):
                    if ln.get(src):
                        jvals[idos.JOB_EXTRA_PROPS[src]] = ln[src]
                self._insert(JOB_IDO, jvals, "job", job, f"create_job_{i}")
                self._written.add(i)
                self.results[i - 1]["co_line"] = i

            if not self._apply_read_back(sorted(self._written)):
                bad = "; ".join(f"line {r['line']}: {w}" for r in self.results for w in r["warnings"]
                                if w.startswith("read-back")) or "read-back could not be completed"
                raise _Stop(self._park(errors.READBACK_MISMATCH, bad, "read_back"))
            return self._outcome({"state": "succeeded", "customer_match": self.customer})
        except _Stop as stop:
            return self._outcome(self._settle(stop.outcome))

    def _settle(self, outcome):
        """After a stop: read back what was written (best effort) and classify (research.md R7)."""
        written = sorted(self._written)
        confirmed = self._apply_read_back(written) if written else True
        step = outcome.get("step", "")
        rejected_line = (outcome.get("code") == errors.ERP_REJECTED and step.startswith("create_line_"))
        if (rejected_line and written and confirmed and self._proven_absent_line(step)):
            self.possibly = [p for p in self.possibly if p["key"] != f"{self.co_num}/{step.rsplit('_', 1)[1]}"]
            failed_line = int(step.rsplit("_", 1)[1])
            self.results[failed_line - 1]["status"] = FAILED
            self.results[failed_line - 1]["error"] = outcome["message"]
            return {"state": "partly_saved", "code": outcome["code"], "message": outcome["message"], "step": step,
                    "customer_match": self.customer}
        failed_line = self._line_of(step)
        if failed_line and failed_line not in self._written and outcome["state"] != "failed":
            # a rejected/unknown line insert: FAILED; a stop after the line row went in: a row may exist
            self.results[failed_line - 1]["status"] = FAILED if step.startswith("create_line_") else UNVERIFIED
            self.results[failed_line - 1]["error"] = outcome["message"]
        return outcome

    @staticmethod
    def _line_of(step):
        tail = step.rsplit("_", 1)[-1]
        return int(tail) if step.startswith(("create_line_", "create_job_", "teardown_")) and tail.isdigit() else None

    def _proven_absent_line(self, step):
        """True when neither the CO line nor the job of the line whose insert was rejected exists."""
        i = int(step.rsplit("_", 1)[1])
        job = self.jobs[i - 1]["job"]
        try:
            line_rows = self.soap.read_back(LINE_IDO, ["CoNum", "CoLine"],
                                            f"CoNum = {_q(self.co_num)} AND CoLine = {i}")
            job_rows = self.soap.read_back(JOB_IDO, ["Job"], f"Job = {_q(job)}")
        except Exception:
            return False
        return not line_rows and not job_rows


def run_save(reservation, request, soap, cfg, remaining_ms=None, on_progress=None):
    return SaveRun(reservation, request, soap, cfg, remaining_ms, on_progress).run()
