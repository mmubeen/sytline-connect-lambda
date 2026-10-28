---

description: "Task list for the due date from the customer contract"
---

# Tasks: Due Date from the Customer Contract

**Input**: Design documents from `/specs/012-due-date-from-contract/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/line-result.md

**Tests**: included (the repository tests every behaviour with fakes before deployment).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependencies)
- Python tests: `PYENV_VERSION=3.12.14 python -m pytest ...`

---

## Phase 1: Setup

- [X] T001 Add to `src/syteline/idos.py`: `COPARMS_IDO = "SLCoparms"`, `DUE_PERIOD_PROP = "DuePeriod"` (the contract entry reuses `itemdesc.ITEM_DESC_IDO` = `SLItemcusts`)
- [X] T002 [P] Extend `FakeSoap` in `tests/fakes.py`: `SLItemcusts` rows may carry `CustNum` and `DuePeriod`; an `SLCoparms` table with a `DuePeriod` row; options to refuse either read
- [X] T003 [P] `mock/erp/app.py` needs no change: it is generic over seeded rows (`SLItemcusts` with `CustNum`/`DuePeriod`, `SLCoparms`)

---

## Phase 2: Foundational (blocks all stories)

- [X] T004 Create `src/syteline/duedate.py`: `period(soap, cust_num, item_code, cache=None)` returns `(days, source)`; reads `SLItemcusts` with `CustNum = <rjust 7> AND Item = <repair code>` (`record_cap=1`), uses `DuePeriod` only when it parses to an integer > 0 ("a positive whole number of days"; text, zero, negative, blank = none for that source), else the first `SLCoparms` row's `DuePeriod` under the same rule, else `(None, "none")`; sources `"contract"` | `"default"` | `"none"`; catches every error (never raises, never retried); one read per distinct (customer, item) and one default read per `cache`
- [X] T005 Add `due_date(received, days)` to `src/syteline/duedate.py`: received date (`YYYY-MM-DD`, `MM/DD/YYYY`) plus `days`, returned as `rowbuild.erp_datetime(...)`; `None` when the date is not valid or `days` is not an integer > 0
- [X] T006 [P] `tests/unit/test_duedate.py`: contract hit; contract zero/blank/text falls to the default; default hit; neither gives `(None, "none")`; a refused or unreachable read gives `none` without raising; cache avoids repeat reads; `due_date` adds days across a month and a year end, rejects a bad date and `days <= 0`

**Checkpoint**: the rule exists and is tested on its own.

---

## Phase 3: User Story 1 - Each line gets its due date from the contract (Priority: P1) MVP

**Goal**: the order line carries received date + the contract (else default) due period; a document date is ignored.

**Independent Test**: save a line whose customer/item contract says 30 days; the written line's `due_date` is received + 30.

- [X] T007 [US1] In `src/syteline/savewf.py` add `_due(i, ln, req)`: calls `duedate.period(self.soap, self.customer["cust_num"], rowbuild.repair_item(ln["item"]), self._due_cache)` and `duedate.due_date(rowbuild._received(req), days)`; returns the effective line (`dict(ln)` with `due_date` set to the computed value, or removed when none, so the document's date is never written); records `due_date` and `due_source` on `self.results[i - 1]`; with `none` or an unusable received date appends the warning from `contracts/line-result.md` ("no due date: ..."); `self._due_cache = {}` in `__init__`
- [X] T008 [US1] Use the effective line in both write paths of `savewf.py` (`_overwrite` and the normal create loop) for `rowbuild.co_line` (and, for US2, `job_row`), called once per line before the rows are built
- [X] T009 [US1] Make `due_date` on `CCT_Coitems` always part of the property pre-check in `src/syteline/preflight.py` (`needed` includes it for every save; the existing `test_the_names_to_check...` expectation that `due_date` is absent for plain lines is updated)
- [X] T010 [P] [US1] `tests/unit/test_savewf.py`: contract 30 days gives received + 30 on the written `CCT_Coitems` row and `due_source` `contract`; default fallback gives `default`; a document `due_date` is not written; neither source gives no `due_date` on the row, `due_source` `none` and a line warning; an invalid received date gives the other warning
- [X] T011 [P] [US1] `tests/unit/test_preflight.py`: `due_date` is in the `CCT_Coitems` names for a plain line

**Checkpoint**: lines are dated; MVP.

---

## Phase 4: User Story 2 - The job's schedule follows the same date (Priority: P2)

**Goal**: the job starts and ends on the line's due date; without one, the job date as today.

- [X] T012 [US2] Pass the same effective line to `rowbuild.job_row` in both write paths of `src/syteline/savewf.py` (no change to `rowbuild.job_row`: it already uses `ln["due_date"]` for `JschStartDate`/`JschEndDate`)
- [X] T013 [P] [US2] `tests/unit/test_savewf.py`: the job row's `JschStartDate` and `JschEndDate` equal the line's computed `due_date`; with no due date they equal `JobDate`; in development mode the existing line and job updates carry the computed date

**Checkpoint**: line and job agree.

---

## Phase 5: User Story 3 - A lookup problem never fails the save (Priority: P3)

**Goal**: refused or empty lookups only cost the date.

- [X] T014 [US3] Confirm `_due` in `src/syteline/savewf.py` never raises and never changes the outcome (already by `duedate.period`); log only customer, item, days and source (no other data)
- [X] T015 [P] [US3] `tests/unit/test_savewf.py`: both lookups refused gives a saved order, lines without `due_date`, a warning per line and an unchanged `saved` state; an unreachable ERP during the lookups does not mask the later write's own unreachable handling
- [X] T016 [P] [US3] `tests/unit/test_savewf.py`: the lookups are reads (no write recorded); covered by `test_the_lookups_are_reads_only` instead of test_resilience.py

---

## Phase 6: Polish and cross-cutting

- [X] T017 [P] `docs/SAVE_WORKFLOW.md`: replace the "Not done yet" note on the due period with the rule (contract, then company default, received date + days, document date ignored, job dates follow), the result fields and the warning; keep teardown/UOM listed as not done; update the `rowbuild.py` module docstring the same way
- [X] T018 [P] Add `SLCoparms` / `SLItemcusts` `DuePeriod` and `CustNum` to the `DEFAULTS` of `src/syteline/probe.py` (so `probe_properties` can check them) and extend `tests/unit/test_probe.py`
- [X] T019 Run the full suite: `PYENV_VERSION=3.12.14 python -m pytest tests -q --deselect tests/unit/test_sample_event.py --ignore=tests/unit/mock/test_make_guards.py`
- [ ] T020 Run `tests/unit/mock/test_make_guards.py` and the mock-environment save with the Docker mock (needs Docker) and confirm the line shows the computed date
- [ ] T021 Deployment: `probe_properties` for `SLItemcusts` (`CustNum`, `Item`, `DuePeriod`), `SLCoparms` (`DuePeriod`) and `CCT_Coitems` (`due_date`); a dev save against `DEV_TARGET_CO` with a customer/item that has a contract period, then one without; if the padded `CustNum` filter returns nothing, switch it to the demo's `LTRIM(CustNum) = '<cust>'` form

---

## Dependencies and order

- Phase 1 then Phase 2 (T004, T005 before T006) then US1 (T007 → T008 → T009; tests T010, T011), US2 (T012 after T008), US3 (T014 after T007), Polish.
- US2 and US3 depend on the US1 effective-line step; they are otherwise independent of each other.

## Parallel examples

- T002, T003 together; T006, T010, T011 (different test files) together once their code exists; T017, T018 together.

## Implementation strategy

MVP is US1 (T001-T011): lines dated. US2 is a one-line change on top. US3 mostly proves behaviour already built in. T020/T021 need Docker and the real ERP.
