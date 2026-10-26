# Tasks: Development Mode Against a Pre-Created Customer Order

**Input**: `/specs/009-dev-target-co/` (plan.md, spec.md, research.md, data-model.md, contracts/dev-mode.md, quickstart.md)

**Tests**: included (the project is tests-first with fakes; SC-004 requires the off-mode tests to stay green).

Format: `- [ ] T### [P?] [US?] description with path`. Paths are relative to the repo root.

## Phase 1: Setup

- [X] T001 Replace `fixed_co_number()` in `src/syteline/config.py` with `dev_target_co()`: reads `DEV_TARGET_CO`, returns `""` when empty; a non-empty value not matching `^[0-9]{1,10}$` raises `IntegrationError` code `DEV_TARGET_INVALID`; add `DEV_TARGET_INVALID`, `DEV_TARGET_MISSING`, `DEV_TARGET_CUSTOMER_MISMATCH` to `src/syteline/errors.py`
- [X] T002 [P] Replace the `CoNumber`/`CO_NUMBER` parameter, env var and description in `src/template.yaml` with `DevTargetCo` (`AllowedPattern: '^([0-9]{1,10})?$'`, default empty) -> env `DEV_TARGET_CO`; update `src/deploy.sh` (validation of 1-10 digits, warning naming the target while set, remove CO_NUMBER handling) and `src/deploy.env.example`
- [X] T003 [P] Update `tests/unit/test_template_single.py`, `tests/unit/test_deploy_script.py`, `tests/unit/test_config.py` for the renamed setting (valid, invalid, empty, warning text)

## Phase 2: Foundational (blocks all stories)

- [X] T004 Create `src/syteline/devmode.py`: `check_target(soap, target, customer)` reads `SLCos` `CoNum`,`CustNum` with `rowbuild.co_filter(target)`; none -> `IntegrationError(DEV_TARGET_MISSING, step="check_target")`; padded `CustNum` != `idos.erp_cust_num(customer["cust_num"])` -> `DEV_TARGET_CUSTOMER_MISMATCH` naming both customers; `marker(target)` returns `{"development": {"target": target}}`
- [X] T005 [P] Add `dev_target` optional attribute to `ReservationStore.create_reservation` in `src/syteline/reservations.py` (new optional keyword `dev_target=""`, stored only when non-empty) and `claim_dev(document_id, execution, dev_target)` to `src/syteline/eventstore.py` (unconditional put of the claim item, state `processing`, attribute `dev_target`)
- [X] T006 [P] Add `update` support for `SLCos`, the line IDO and `CCTJobs` rows to the fake in `tests/fakes.py` and the mock ERP in `mock/erp/app.py` (update by key properties, error when the row is absent)
- [X] T007 [P] Unit tests for T001/T004/T005 in `tests/unit/test_devmode.py` (target exists / missing / other customer; invalid setting; `claim_dev` overwrites an existing claim)

**Checkpoint**: setting, target check, store support and fakes ready.

## Phase 3: User Story 1 - Overwrite the same pre-created order (P1) MVP

**Goal**: reservation uses the target; save updates header/lines (and job) instead of inserting.
**Independent Test**: set the target, save a document twice with different values; one order, latest values, no number allocated, no "already exists".

- [X] T008 [P] [US1] Tests in `tests/unit/test_reserve.py`: with `DEV_TARGET_CO` set, no `next_co` call, `co_num` = target, response has `development.target`, `co_source` `development`, with `JOB_NUMBER` also no `next_job` call; target missing/mismatch -> failure with the new codes, `consumed_unused` absent
- [X] T009 [P] [US1] Tests in `tests/unit/test_savewf.py`: dev mode updates the header, updates existing lines, inserts missing lines, reports `surplus_lines`, updates an existing job when `JOB_NUMBER` is set else inserts, never calls delete, target re-checked before the first write (missing/mismatch -> `failed`, nothing written), second run overwrites the first; off mode: insert-only tests unchanged
- [X] T010 [US1] In `src/syteline/reserve.py` replace the `config.fixed_co_number()` use: when `config.dev_target_co()` is set call `devmode.check_target` after customer resolution and before allocating anything (refusal -> `_Stop` with the code, step `check_target`), use the target as `co_num`, set `co_source` `development` and pass `dev_target` to the store; add `development` to the returned dict
- [X] T011 [US1] In `src/syteline/savewf.py` add the dev branch: re-run `devmode.check_target`; header via `soap.update(CO_IDO, {"CoNum": co}, values)` (without `CoNum` duplicated in values); per line read existing lines once (`rowbuild.co_filter`), `update` the line IDO row when present else `_insert`; job: when `config.fixed_job_number()` and the job row exists `update` else `_insert`; keep `_mark` before each write and the same stop/park classification; record `action` per line, `co_action`, `surplus_lines` plus a warning in the outcome; off mode path untouched
- [X] T012 [US1] Add `development` and `co_action` to the save outcome persisted by `src/handlers/worker.py` and the status responses in `src/handlers/` that return reservation/save views (`src/handlers/reserve.py`, `src/handlers/save_status.py` if present)

**Checkpoint**: US1 independently testable with the fakes.

## Phase 4: User Story 2 - Tracker never blocks a development run (P1)

**Goal**: the same document is processed again every time.
**Independent Test**: send the same document twice in dev mode; both end `reserved`; off mode still returns `already_processed`.

- [X] T013 [P] [US2] Tests in `tests/unit/test_event_intake.py`: dev mode with an earlier finished reservation and an existing claim -> processed again (`reserved`), tracker rows still written with `dev_target`; earlier uncertain/failed entry does not block; off mode `already_processed` unchanged
- [X] T014 [US2] In `src/handlers/event_intake.py`: when `config.dev_target_co()` is set skip the `first is not None and _final(first)` short-circuit, claim with `events.claim_dev(...)`, and add `dev_target` to every `_log` call and `_out` of the run; `DEV_TARGET_INVALID` from `config` is returned as a refusal before the claim
- [X] T015 [US2] Check `src/handlers/reserve.py` (direct API): in dev mode a repeated `request_ref` still returns the duplicate (caller's choice); add a note to the response only; no code change if the test in T013 style confirms it

**Checkpoint**: US1 + US2 give repeatable development runs.

## Phase 5: User Story 3 - Only the target can be touched, and it is visible (P2)

**Goal**: guard rails and marking.

- [X] T016 [P] [US3] Tests in `tests/unit/test_savewf.py`/`test_devmode.py`: every ERP write of a dev save names only the target CO (assert on the fake's write log); logs and responses of a dev run contain `dev_target`; `deploy.sh` warning shown only when set
- [X] T017 [US3] Add `dev_target` to the structured log fields in `src/syteline/eventlog.py` / `src/handlers/worker.py` log calls so every log line of a dev run names the target (FR-009)
- [ ] T018 (not done: needs Docker, not run here) [US3] Mock environment: add scenario `dev-target` to `mock/scenarios/generate.py` (pre-created order for the document's customer, repeated sends) and a mock-env test in `tests/mock_env/` asserting overwrite, no new order, no counter movement; make `mock/render_env.py` pass `DEV_TARGET_CO` from the template parameter

## Phase 6: Polish

- [X] T019 [P] Update `docs/EVENT_INTAKE.md` (replace the `CO_NUMBER` section with `DEV_TARGET_CO`: what it does, the customer check, warnings), `docs/SAVE_WORKFLOW.md` (update-or-insert in dev mode), `docs/DEPLOYMENT.md`, and remove `CO_NUMBER` mentions from `src/syteline/probe.py` docstrings if present
- [X] T020 Run `pytest tests/unit tests/contract` (deselect the two Docker `test_make_guards.py` tests) and confirm the existing off-mode tests pass unchanged (SC-004)
- [ ] T021 Deployment-only check (unchecked until deployed): with a real pre-created order, run quickstart steps 3-6 and record whether the ERP accepts `SaveDataSet` updates for the CO header, line and job; fix any key/property message it returns

## Dependencies

Phase 1 -> Phase 2 -> US1 (T010 needs T004/T005; T011 needs T004/T006) -> US2 (T014 needs T005) -> US3. US2 can start after Phase 2 in parallel with US1. T008/T009/T013/T016 tests are written before their implementation tasks.

## Parallel examples

- Phase 1: T002 and T003 after T001.
- Phase 2: T005, T006, T007 together after T004.
- US1: T008 and T009 together; US2 tests T013 can run alongside US1 implementation.

## Strategy

MVP = Phases 1-3 (overwrite works on the fakes); add US2 for repeatability (both are P1, deliver together), then US3 and polish. T021 needs the real ERP.
