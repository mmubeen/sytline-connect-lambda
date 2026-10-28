# Implementation Plan: Due Date from the Customer Contract

**Branch**: `012-due-date-from-contract` | **Date**: 2026-10-28 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/012-due-date-from-contract/spec.md`

## Summary

The save computes each line's due date the way the PO OCR demo does: received date + a due period in days. The period is read (read-only) from the customer's contract entry for the line's repair item (`SLItemcusts`, `CustNum` + `Item`, property `DuePeriod`), falling back to the ERP's company-wide default (`SLCoparms.DuePeriod`), else none. The computed date replaces any `due_date` taken from the document, is written to the order line (`due_date`) and is the job's scheduled start and end (`JschStartDate`/`JschEndDate`, as the existing job row already does when a line has a due date). A lookup that fails or finds nothing never fails the save: the line is written without a due date and a warning names it. Each line result reports `due_date` and `due_source` (`contract` | `default` | `none`).

## Technical Context

**Language/Version**: Python 3.13 (tests on 3.12), AWS SAM Lambda (GovCloud)

**Primary Dependencies**: existing SOAP client (`read_back`/`load`); no new dependency

**Storage**: none new

**Testing**: pytest with `FakeSoap` (`SLItemcusts` rows gain `CustNum`/`DuePeriod`; a `SLCoparms` row) and the mock ERP

**Target Platform**: the single existing Lambda (`work_save` path, also the dev-target overwrite)

**Project Type**: serverless integration function

**Performance Goals**: at most 1 contract read per distinct item plus 1 default read per save; reads reused across lines

**Constraints**: reads only; never retried, never change the outcome; no document text logged

**Scale/Scope**: up to 25 lines per order

## Constitution Check

`.specify/memory/constitution.md` is still the unfilled template; the repository's standing rules apply: live writes off by default (no new write path), no secrets/document text in logs (only customer, item, days, source), uncertain writes never retried (reads only), no deletes. **Result: pass.**

## Project Structure

### Documentation (this feature)

```text
specs/012-due-date-from-contract/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/line-result.md
└── tasks.md             # /speckit-tasks
```

### Source Code (repository root)

```text
src/syteline/
├── duedate.py      # NEW: period(soap, cust_num, item_code) -> (days, source); due_date(received, days); a per-save cache
├── savewf.py       # + _due(i, ln): sets the effective line (computed due_date replaces the document's), warns, records due_date/due_source
├── rowbuild.py     # (unchanged: co_line / job_row already carry ln["due_date"]; date_plus helper if kept here)
├── preflight.py    # + "due_date" on CCT_Coitems is always checked (it is now always written when computed)
└── idos.py         # + CONTRACT_IDO ("SLItemcusts" via itemdesc), COPARMS_IDO = "SLCoparms", DUE_PERIOD_PROP = "DuePeriod"
tests/unit/         # test_duedate.py (new), test_savewf.py, test_preflight.py
tests/fakes.py      # SLItemcusts CustNum/DuePeriod, SLCoparms rows
mock/erp/app.py     # DuePeriod on the contract and parms mock tables
docs/SAVE_WORKFLOW.md
```

**Structure Decision**: one small new module for the rule; the save only asks it for a date per line.

## Complexity Tracking

No constitution violations to justify.
