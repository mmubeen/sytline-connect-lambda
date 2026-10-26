"""Stage 2 of the job-number workflow: save the order information using the numbers reserved in stage 1.

Follows the PO OCR demo's per-line order (runner._process_line) on top of the existing upload bookkeeping:

  CO header -> per line: CO line (CCT_Coitems) carrying its job number (ref_num) -> teardown date (UserDefinedFields row) -> job
  -> read back every saved line, job and the link between them.

It never obtains a number (the reservation already holds them). Safety rules are those of the upload (FR-004 of
feature 001): stop at the first problem, record a row as possibly written BEFORE sending it, never delete,
never retry. Outcome states: saved | partly_saved | failed | needs_reconciliation (research.md R7, R8).
"""

from . import config, defaults, devmode, errors, idos, itemdesc, preflight, rowbuild
from .errors import ErpError, ErpRejected, ErpUncertain, ErpUnreachable, IntegrationError, LiveWritesDisabled
from .pipeline import CO_IDO, JOB_IDO, LINE_IDO, Upload, _Stop

SAVED, FAILED, NOT_ATTEMPTED, UNVERIFIED = "saved", "failed", "not_attempted", "unverified"


class SaveRun(Upload):
    def __init__(self, reservation, request, soap, cfg, remaining_ms=None, on_progress=None):
        super().__init__(request, soap, cfg, remaining_ms, on_progress)
        self.reservation = reservation
        self.customer = reservation.get("customer_match") or {}
        self.co_num = str(reservation["co_num"])
        self.jobs = [{"line": int(j["line"]), "job": str(j["job"])} for j in reservation["job_numbers"]]
        self.results = [{"line": j["line"], "job": j["job"], "item": rowbuild.repair_item(ln["item"]), "status": NOT_ATTEMPTED,
                         "warnings": []} for j, ln in zip(self.jobs, request["lines"])]
        self._written = set()  # lines whose line row and job row were both accepted (not yet read back)
        self.warnings = []     # order-level warnings (for example: customer defaults not available)
        self.dev = ""          # the development target (DEV_TARGET_CO) while development mode is on: rows are overwritten
        self.surplus = []      # lines the target has that the document does not

    # -- bookkeeping -----------------------------------------------------------------------
    def _mark(self, kind, key):
        self.possibly.append({"kind": kind, "key": key})
        if self.on_progress:
            self.on_progress(list(self.possibly), [dict(r) for r in self.results])

    def _outcome(self, base):
        out = dict(base, results=[dict(r) for r in self.results], co_num=self.co_num, warnings=list(self.warnings))
        if self.dev:
            out.update(devmode.marker(self.dev), co_action="updated")
            if self.surplus:
                out["surplus_lines"] = list(self.surplus)
        return out

    def _update(self, ido, key, values, kind, label, step):
        """Overwrite one existing row (development mode). Recorded as possibly written BEFORE the request is sent; a definite
        rejection of the very first write means nothing changed."""
        self._time(step)
        first = not self.possibly
        self._mark(kind, label)
        try:
            return self.soap.update(ido, key, values)
        except LiveWritesDisabled:
            self.possibly.pop()
            raise _Stop(self._failed(errors.LIVE_WRITES_DISABLED, "live ERP writes are disabled", step))
        except ErpUnreachable as exc:
            self.possibly.pop()
            raise _Stop(self._failed(errors.ERP_UNREACHABLE, f"{step}: {exc}", step) if first
                        else self._park(errors.ERP_UNREACHABLE, f"{step}: {exc}", step))
        except ErpRejected as exc:
            if first:
                self.possibly.pop()
                raise _Stop(self._failed(errors.ERP_REJECTED, f"{step}: {exc}", step))
            raise _Stop(self._park(errors.ERP_REJECTED, f"{step}: {exc}", step))
        except ErpUncertain as exc:
            raise _Stop(self._park(errors.UNCERTAIN_OUTCOME, f"{step}: {exc}", step))

    def _description(self, i, ln):
        """The line's description from the item master ("" and a warning on the line when there is none). A read: it is not a write,
        is never retried and never changes the outcome."""
        code = rowbuild.repair_item(ln["item"])
        text = itemdesc.lookup(self.soap, code)
        if text is None:
            self.results[i - 1]["warnings"].append(
                f'no description found in the item master for "{code}" (the line was saved with a blank description)')
        return text or ""

    def _read(self, ido, props, filter_, step):
        """A read made during an overwrite: a failure stops the run (nothing written yet: failed; otherwise: to reconcile)."""
        try:
            return self.soap.read_back(ido, props, filter_)
        except Exception as exc:
            out = self._park if self.possibly else self._failed
            raise _Stop(out(errors.ERP_UNREACHABLE if not self.possibly else errors.UNCERTAIN_OUTCOME, f"{step}: {exc}", step))

    def _overwrite(self, req, found):
        """Development mode: update the target's header and lines (inserting the lines it lacks) and the job when JOB_NUMBER
        names one that exists. Rows are never deleted."""
        header = rowbuild.co_header(self.co_num, self.customer, req, found)
        key = {"CoNum": header["CoNum"]}
        existing = {int(rowbuild.line_numbers(r.get("CoLine")) or 0)
                    for r in self._read(LINE_IDO, ["CoNum", "CoLine"], rowbuild.co_filter(self.co_num), "read_target_lines")}
        self._update(CO_IDO, key, {k: v for k, v in header.items() if k != "CoNum"}, "co", self.co_num, "update_co")
        self.surplus = sorted(n for n in existing if n and n > len(req["lines"]))
        if self.surplus:
            self.warnings.append(f"the development target has lines the document does not ({', '.join(map(str, self.surplus))}); left as they are")
        whse = req.get("warehouse") or self.cfg.get("SYTELINE_DEFAULT_WHSE", "")
        for entry, ln in zip(self.jobs, req["lines"]):
            i, job = entry["line"], entry["job"]
            desc = self._description(i, ln)
            row = rowbuild.co_line(self.co_num, i, ln, job, whse, repair=True, description=desc)
            jrow = rowbuild.job_row(self.co_num, i, ln, job, req, whse, repair=True, description=desc)
            job_there = bool(config.fixed_job_number() and self._read(JOB_IDO, ["Job"], rowbuild.job_filter(job), f"read_job_{i}"))
            jkey = {k: jrow[k] for k in ("Job", "Suffix")}
            if job_there:  # first: the ERP refuses a line whose item differs from the item of the job it references
                self._update(JOB_IDO, jkey, {k: v for k, v in jrow.items() if k not in jkey}, "job", job, f"update_job_{i}")
            if i in existing:
                lkey = {k: row[k] for k in ("co_num", "co_line", "co_release")}
                self._update(idos.LINE_WRITE_IDO, lkey, {k: v for k, v in row.items() if k not in lkey}, "line",
                             f"{self.co_num}/{i}", f"update_line_{i}")
                self.results[i - 1]["action"] = "updated"
            else:
                self._insert(idos.LINE_WRITE_IDO, row, "line", f"{self.co_num}/{i}", f"create_line_{i}")
                self.results[i - 1]["action"] = "inserted"
            if "teardown" in ln:
                self._teardown(i, ln["teardown"])
            if not job_there:
                self._insert(JOB_IDO, jrow, "job", job, f"create_job_{i}")
            self._written.add(i)
            self.results[i - 1]["co_line"] = i

    # -- steps -----------------------------------------------------------------------------
    def _teardown(self, i, value):
        """Set the line's teardown date as the demo does: a UserDefinedFields row for the line's RowPointer. A definite rejection
        is a warning; anything else leaves the outcome unknown and stops the run."""
        step = f"teardown_{i}"
        self._time(step)
        try:
            rowbuild.set_teardown(self.soap, self.co_num, i, value)
        except ErpRejected as exc:
            self.results[i - 1]["warnings"].append(f"teardown date not set: {exc}")
        except LiveWritesDisabled:
            raise _Stop(self._park(errors.LIVE_WRITES_DISABLED, "live ERP writes are disabled", step))
        except ErpError as exc:
            raise _Stop(self._park(errors.UNCERTAIN_OUTCOME, f"{step}: {exc}", step))

    def _read_back(self, lines):
        """{line: problem or None} for the given line numbers, plus a header problem; None if the read failed."""
        try:
            cos = self.soap.read_back(CO_IDO, ["CoNum", "CustNum"], rowbuild.co_filter(self.co_num))
            rows = self.soap.read_back(LINE_IDO, ["CoNum", "CoLine", "Item", "QtyOrdered", idos.LINK_NUM_PROP],
                                       rowbuild.co_filter(self.co_num))
            found = {j["job"]: self.soap.read_back(JOB_IDO, ["Job", *idos.JOB_LINK_PROPS.values()], rowbuild.job_filter(j["job"]))
                     for j in self.jobs if j["line"] in lines}
        except Exception:
            return None
        header = None
        if len(cos) != 1 or str(cos[0].get("CoNum", "")).strip() != self.co_num:
            header = "CO header not found or duplicated"
        elif str(cos[0].get("CustNum", "")).strip() != str(self.customer.get("cust_num", "")):
            header = "CO customer differs from the resolved customer"
        by_line = {str(r.get("CoLine")).strip(): r for r in rows}
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
                if str(row.get("Item", "")).strip() != rowbuild.repair_item(ln["item"]):
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
            elif (str(job_rows[0].get(idos.JOB_LINK_PROPS["co_num"], "")).strip() != self.co_num
                  or str(job_rows[0].get(idos.JOB_LINK_PROPS["co_line"], "")).strip() != str(i)):
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
            long_lines = rowbuild.item_too_long(req["lines"])
            if long_lines:  # the item code would not fit the ERP's item width: refused, never truncated
                raise _Stop(self._failed(errors.ITEM_TOO_LONG, "item code longer than 30 characters (with the R* prefix) on line "
                                         + ", ".join(map(str, long_lines)), "check_items"))
            self.dev = self.reservation.get("dev_target") or ""  # the reservation was made in development mode
            try:
                current = config.dev_target_co()
            except IntegrationError as exc:
                raise _Stop(self._failed(exc.err["code"], exc.err["message"], "check_target"))
            if self.dev and current != self.dev:
                raise _Stop(self._failed(errors.DEV_TARGET_INVALID,
                                         f"the development target changed since the reservation ({self.dev} -> {current or 'off'}); reserve again",
                                         "check_target"))
            if self.dev:  # the target must exist and be the document's customer's, checked again right before the first write
                try:
                    note = devmode.check_target(self.soap, self.dev, self.customer)
                    if note:
                        self.warnings.append(note)
                except IntegrationError as exc:
                    raise _Stop(self._failed(exc.err["code"], exc.err["message"], "check_target"))
                except ErpError as exc:
                    raise _Stop(self._failed(errors.ERP_UNREACHABLE, f"could not check the development target: {exc}", "check_target"))
            if preflight.enabled():  # nothing is written for a save the ERP would refuse halfway
                try:
                    missing = preflight.check(self.soap, req["lines"])
                except ErpError as exc:
                    raise _Stop(self._failed(errors.ERP_UNREACHABLE, f"could not check the ERP's property names: {exc}",
                                             "check_properties"))
                if missing:
                    raise _Stop(self._failed(errors.ERP_PROPERTIES_UNKNOWN, preflight.message(missing), "check_properties"))
            # the customer's terms and ship-via from its most recent order; never blocks the save (warning instead)
            found, warning = defaults.read(self.customer, self.soap, self.cfg)
            if warning:
                self.warnings.append(warning)
            if self.dev:
                self._overwrite(req, found)
            else:
                self._insert(CO_IDO, rowbuild.co_header(self.co_num, self.customer, req, found), "co", self.co_num, "create_co")
                whse = req.get("warehouse") or self.cfg.get("SYTELINE_DEFAULT_WHSE", "")
                for entry, ln in zip(self.jobs, req["lines"]):
                    i, job = entry["line"], entry["job"]
                    desc = self._description(i, ln)
                    self._insert(idos.LINE_WRITE_IDO, rowbuild.co_line(self.co_num, i, ln, job, whse, repair=True, description=desc), "line",
                                 f"{self.co_num}/{i}", f"create_line_{i}")
                    if "teardown" in ln:
                        self._teardown(i, ln["teardown"])
                    self._insert(JOB_IDO, rowbuild.job_row(self.co_num, i, ln, job, req, whse, repair=True, description=desc), "job", job, f"create_job_{i}")
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
        return int(tail) if step.startswith(("create_line_", "create_job_", "teardown_", "update_line_", "update_job_")) and tail.isdigit() else None

    def _proven_absent_line(self, step):
        """True when neither the CO line nor the job of the line whose insert was rejected exists."""
        i = int(step.rsplit("_", 1)[1])
        job = self.jobs[i - 1]["job"]
        try:
            line_rows = self.soap.read_back(LINE_IDO, ["CoNum", "CoLine"],
                                            f"{rowbuild.co_filter(self.co_num)} AND CoLine = {idos.quote(rowbuild.rjust(i, rowbuild.CO_LINE_WIDTH))}")
            job_rows = self.soap.read_back(JOB_IDO, ["Job"], rowbuild.job_filter(job))
        except Exception:
            return False
        return not line_rows and not job_rows


def run_save(reservation, request, soap, cfg, remaining_ms=None, on_progress=None):
    return SaveRun(reservation, request, soap, cfg, remaining_ms, on_progress).run()
