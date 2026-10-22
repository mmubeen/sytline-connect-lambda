---

description: "Task list for Job Number Reservation and Save Workflow"
---

# Tasks: Job Number Reservation and Save Workflow

**Input**: Design documents from `/specs/004-job-number-workflow/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/api.md, quickstart.md

**Tests**: Included (plan.md specifies pytest unit and contract tests with the existing fakes in `tests/fakes.py`, plus template structure tests and `sam validate --lint`). No real ERP or deployment from this environment.

**Organization**: Grouped by user story. Paths are relative to the repo root. The existing one-step upload, notes and health actions (`scr/syteline/pipeline.py`, `handlers/intake.py`, `handlers/status.py`, `lambda_function.py`) must keep their behavior (FR-017): extend shared files only by adding, and run the full suite after each phase.

**Hard rules**: ERP only through the web service, no SQL (FR-015, `tests/unit/test_no_sql.py` must stay green); reserving and every write are gated by live writes (FR-014); never delete, retry or replay a write whose outcome is unknown (FR-010); no secrets in logs or responses (`tests/unit/test_no_secrets.py`); `UNCONFIRMED` ERP names live only in `scr/syteline/idos.py`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1 = get job numbers (P1), US2 = save using the reserved numbers (P1), US3 = safe handling of reservations and failures (P2), US4 = look up what happened (P3)

---

## Phase 1: Setup

- [X] T001 [P] Add the new error codes to `scr/syteline/errors.py` as constants: `TOO_MANY_LINES`, `RESERVATION_USED`, `RESERVATION_MISMATCH`, `RESERVATION_FORBIDDEN`, `RESERVATION_NOT_READY` (existing codes are reused unchanged)
- [X] T002 [P] Add to `scr/syteline/idos.py` the property names the save needs, each marked `UNCONFIRMED` like the existing ones and overridable the same way where the file already allows it: header customer PO number and received date, line `ref_num` link to the job and teardown property, job `RootJob`, engine serial, serial number and packing (research.md R8, R9)

---

## Phase 2: Foundational (blocks all user stories)

- [X] T003 Create `scr/syteline/reservations.py`: `ReservationStore` over the existing `DynamoTable` adapter in `scr/syteline/jobs.py` (reuse `DynamoTable`, `_cond_failed`, `RefConflict`, `TTL_AFTER_TERMINAL`; do not copy them). States exactly: `reserving`, `reserved`, `save_accepted`, `saving`, `saved`, `partly_saved`, `failed`, `needs_reconciliation`; terminal = `saved`, `partly_saved`, `failed`, `needs_reconciliation`. Methods: `create_reservation(request, payload_hash, fingerprint, uploaded_by, caller_identity)` writing `RESERVATION#<id>` (state `reserving`, `job_numbers: []`) and `RESREF#<request_ref>` atomically (conditional `attribute_not_exists(pk)` on the index; same ref + same hash returns `(existing, True)`, different hash raises `RefConflict`); `get(reservation_id)`; `set_co_num`; `add_job_number` (writes the whole `job_numbers` list; the reserving run is the single writer, so no list-append is needed); `mark_reserved`; `mark_failed`; `accept_save` (conditional `reserved -> save_accepted`, stores `save: {requested_at, payload_hash, request}`); `claim` (`save_accepted -> saving`, increments `attempts`); `record_progress`; `finish_saved`, `finish_partly_saved`, `park` (to `needs_reconciliation`); `list_save_accepted_older_than`, `list_saving_older_than`. `ttl` is set only on terminal states. Field names per data-model.md
- [X] T004 [P] Add `tests/unit/test_reservations_store.py` against `FakeJobsTable` (extend `tests/fakes.py` only if a needed expression is missing): atomic create, same ref/same hash returns existing, same ref/different hash raises `RefConflict`, `accept_save` succeeds once and fails the second time, `claim` only from `save_accepted`, nothing leaves `needs_reconciliation`, `ttl` only when terminal
- [X] T005 [P] Add an `update(ido, key, values)` operation to `SoapIdoClient` in `scr/syteline/client.py`: gated by `config.live_writes_enabled()` (raises `LiveWritesDisabled`), sends an Update change like `insert` does, row-level error raises `ErpRejected`, a transport failure after sending raises `ErpUncertain`, a failure while connecting `ErpUnreachable` (same classification as `insert`)
- [X] T006 [P] Add `update` to `FakeSoap` in `tests/fakes.py` with the same scenario mechanism (`("update", ido, nth)`: `reject`, `timeout_after`, `timeout_before`, `unreachable`), applying the change to the stored row and counted in `writes()`; add a test for `update` (live-writes gate, rejection, uncertain) in `tests/unit/test_resilience.py`
- [X] T007 [P] Add `caller_identity(event)` to `scr/handlers/_common.py`: returns `requestContext.identity.userArn`, or `"unknown"`; for an assumed role (`arn:...:assumed-role/<Role>/<session>`) drop the session name so the same role matches across sessions (research.md R6); test it in `tests/unit/test_logging.py` or a new `tests/unit/test_caller_identity.py`

**Checkpoint**: store, client `update`, fake and caller identity exist; the whole existing suite is still green.

---

## Phase 3: User Story 1 - Get job numbers from the ERP (Priority: P1) 🎯 MVP

**Goal**: `POST /job-reservations` returns one CO number and one job number per line, written nowhere else.

**Independent Test**: reserve for a 3-line PO against `FakeSoap`; three distinct job numbers and one CO number come back in line order, and `FakeSoap.writes()` shows no insert and no update.

- [X] T008 [P] [US1] Add `tests/unit/test_reserve.py` using `FakeSoap` and `FakeJobsTable`: numbers in line order and no inserts (acceptance 1); two concurrent reserves never share a number (SC-002, use the pattern of `tests/unit/test_concurrency.py`); no numbers when the customer is not found, ambiguous or not a confident single match, and not even a reservation item is left in `reserved` (acceptance 3); live writes off reserves nothing and returns `LIVE_WRITES_DISABLED` (acceptance 4, SC-005); same `request_ref` and payload returns the same numbers without a new allocation; ERP failure mid-way ends `failed` with the numbers already obtained listed `consumed_unused`; time budget exhausted (inject `remaining_ms`) does the same with `TIME_BUDGET_EXHAUSTED`; more than 25 lines is `TOO_MANY_LINES`
- [X] T009 [P] [US1] Add reserve cases to `tests/contract/test_reservation_routes.py` (new file) for `POST /job-reservations` per `contracts/api.md`: 201, 200 with `duplicate: true`, 400, 409, 422, 502, 503 and the exact response bodies
- [X] T010 [US1] Add `validate_reserve(body)` and `fingerprint(customer, po_number, line_count)` to `scr/syteline/validation.py`: `request_ref` 1-128 chars; `confirmed` must be `true`; `uploaded_by` non-empty; `customer.name` required, address fields strings, no customer-number keys (reuse the existing customer checks, do not duplicate them); `po_number` non-empty string; `line_count` integer from 1 to 25 else `TOO_MANY_LINES` (or `VALIDATION` when not an integer or less than 1); all problems reported together. `fingerprint` is the SHA-256 of normalized customer name, address1, city, state, postal code, the PO number and the line count (use `resolver.normalize`)
- [X] T011 [US1] Create `scr/syteline/reserve.py` with `run_reserve(request, soap, store, caller_identity, remaining_ms)`: order is (1) create the reservation and index (T003), duplicate returns the existing reservation; (2) resolve the customer with `resolver.resolve_customer` (failure marks the reservation `failed` with the resolver's code, nothing reserved); (3) live-writes check before any number is requested; (4) obtain the CO number with `idos.next_co()` then one job number per line with `idos.next_job()` via `soap.invoke`, checking `MIN_REMAINING_MS` (import it from `pipeline.py`) before each, and recording each number with `add_job_number` as soon as it is obtained; (5) `mark_reserved`. Errors map as in `pipeline.Upload._allocate` (`ErpRejected` -> `ERP_REJECTED`, other `ErpError` -> `ERP_UNREACHABLE`, empty result -> `ERP_REJECTED`); a failure after some numbers were obtained ends `failed` and returns them as `consumed_unused`. Never writes a CO, line or job row
- [X] T012 [US1] Create `scr/handlers/reserve.py` (`handler`) following `handlers/intake.py`: parse JSON, `validate_reserve`, call `run_reserve` with `caller_identity(event)` and the context's remaining time, map outcomes to the status codes in `contracts/api.md`, log with `log_event("reserve", ...)` (request_ref, outcome, reservation_id, uploaded_by, caller, co_num; never customer details beyond what `intake.py` logs); overridable `_store_factory`/`_clients_factory` for tests like the other handlers
- [X] T013 [US1] Add to `scr/template.yaml`: `ReserveFunction` (handler `handlers.reserve.handler`, `Timeout: 28`, same `VpcConfig`, ERP environment variables, `SYTELINE_LIVE_WRITES` and `DynamoDBCrudPolicy` on `JobsTable` plus the `secretsmanager:GetSecretValue` statement as `WorkerFunction`; no `ReservedConcurrentExecutions` serialization needed because the ERP's numbering routines are transactional) and a `POST /job-reservations` event on `OrderApi`. Do not change any existing resource
- [X] T014 [P] [US1] Add `tests/unit/test_template_reservation.py` (new) with the reserve checks using `tests/cfn_template.py`: the function is in the VPC, has the secret and table permissions, the route exists on `OrderApi`, no new `AWS::EC2::*` resource appeared (compare the set with `tests/unit/test_template_network.py`'s expectations) and `Timeout` is below 29

**Checkpoint**: a caller can reserve numbers; nothing is written to the ERP except the consumed numbers. Run `python -m pytest tests -q`.

---

## Phase 4: User Story 2 - Save the order information using the reserved numbers (Priority: P1)

**Goal**: `POST /job-reservations/{id}/save` saves header, lines (carrying their job numbers), teardown dates and jobs, then verifies by read-back.

**Independent Test**: reserve for a 2-line PO, save it; `FakeSoap` holds one header, two lines with their job number link, two jobs linked back to their lines, and the status shows `saved`.

- [X] T015 [P] [US2] Add `tests/unit/test_savewf.py`: full happy path with order header, line (`ref_num` = job number), teardown update and job per line, in that order, using exactly the reserved numbers and requesting no new numbers (FR-005, assert no `invoke` calls); per-line results carry job, CO line, item (acceptance 1); read-back confirms the CO line, the job and the link, and a missing or differing row ends `needs_reconciliation` with `READBACK_MISMATCH` (SC-003); a teardown `reject` becomes a warning on that line, the job still saves and the reservation still ends `saved` (acceptance 3); a teardown `timeout_after` ends `needs_reconciliation` (research.md R8); live writes off ends `failed` before any write
- [X] T016 [P] [US2] Add save-intake cases to `tests/contract/test_reservation_routes.py` for `POST /job-reservations/{id}/save`: 202 with `state: save_accepted`, 400 on validation, 404 unknown id, and that the worker is invoked asynchronously once (use `FakeLambda`)
- [X] T017 [US2] Add `validate_save(body)` to `scr/syteline/validation.py`: `confirmed` true, `uploaded_by` non-empty, `customer` as in reserve, `po_number` non-empty, `lines` non-empty (1 to 25), each line `item` non-empty, `quantity` number greater than 0, `uom` non-empty, optional `due_date` (YYYY-MM-DD), `price` (non-negative), `teardown` (string), `engine_sn`, `serial_num`, `packing` (strings), header optional `order_date` and `received_date` (YYYY-MM-DD), `warehouse`, `order_type` (non-empty strings when supplied); reuse the existing line checks from `validate_order` rather than copying them (refactor into a shared helper without changing `validate_order`'s results; `tests/unit/test_validation.py` must stay green)
- [X] T018 [US2] Create `scr/syteline/savewf.py` with `class SaveRun(Upload)` (import `Upload` from `pipeline.py`; reuse its `_insert`, `_mark`, `_time`, `_failed`, `_park`) and `run_save(reservation, request, soap, cfg, remaining_ms, on_progress)`: take `co_num`, `job_numbers` and `customer_match` from the reservation; check live writes first; insert the CO header (existing `SLCos` values plus customer PO number and received date); for each line in order insert the line with `ref_num` = its job number and link properties, then set the teardown date with `soap.update` (a definite `ErpRejected` is a warning on the line, any other `ErpError` is treated as uncertain and stops the run), then insert the job with `RootJob` and the optional engine/serial/packing values; collect `results[]` (`line, job, co_line, item, status, warnings`); stop at the first problem with the remaining lines `not_attempted` (state classification refined in US3); read back every saved line, job and link and compare as `Upload._verify` does; return the outcome dict (`state`, `results`, `co_num`, ...)
- [X] T019 [US2] Create `scr/handlers/save_intake.py`: read `pathParameters.id`, parse and `validate_save`, load the reservation (404 if missing), `accept_save` (conditional `reserved -> save_accepted`; any failure to transition is `RESERVATION_USED` or `RESERVATION_NOT_READY` per current state, 409, nothing else done), then invoke the worker asynchronously with `{"reservation_id": ...}` exactly like `handlers/intake.py` (a failed invoke is swallowed; the sweeper re-drives); return 202. Overridable `_store_factory`/`_lambda_factory`
- [X] T020 [US2] Extend `scr/handlers/worker.py`: when the event has `reservation_id`, claim the reservation, run `savewf.run_save` with the stored request and persist the outcome (`finish_saved`, `park`, or `mark_failed`) using the same unexpected-exception handling as the upload branch (possibly written present -> `needs_reconciliation`, else `failed`; never auto-retry). The upload branch (`upload_id`) must remain byte-for-byte equivalent in behavior; `tests/unit/test_worker_sweeper.py` stays green
- [X] T021 [US2] Add to `scr/template.yaml`: `SaveIntakeFunction` (handler `handlers.save_intake.handler`, `Timeout: 15`, `WORKER_FUNCTION_NAME: !Ref WorkerFunction`, `DynamoDBCrudPolicy` and `LambdaInvokePolicy` like `IntakeFunction`) with a `POST /job-reservations/{id}/save` event on `OrderApi`; extend `tests/unit/test_template_reservation.py` with the checks for it. `WorkerFunction` already has the policies the save needs (jobs table, secret)

**Checkpoint**: reserve then save works end to end against the fakes. Run `python -m pytest tests -q`.

---

## Phase 5: User Story 3 - Safe handling of reservations and failures (Priority: P2)

**Goal**: a reservation is usable once, for its own PO, by its own caller; failures never delete or replay; unused numbers are accounted for.

**Independent Test**: a second save, a save for a different PO and a save from a different caller are each refused with nothing written; an uncertain save is parked and never re-run.

- [X] T022 [P] [US3] Extend `tests/contract/test_reservation_routes.py` and `tests/unit/test_savewf.py` for: second save -> 409 `RESERVATION_USED` carrying the current state and zero ERP writes (acceptance 1); different customer, PO number or line count -> 422 `RESERVATION_MISMATCH` (acceptance 2); different caller identity or different `uploaded_by` -> 403 `RESERVATION_FORBIDDEN`; save on a `reserving` or `failed` reservation -> 409 `RESERVATION_NOT_READY`; the same role with a different session name is accepted; timeout during the save -> `needs_reconciliation` and the sweeper does not re-drive it (acceptance 3); definite rejection with the failed line's rows proven absent by read-back -> `partly_saved`, earlier lines individually verified, later lines `not_attempted`; rejection where absence cannot be proven -> `needs_reconciliation`; failure-injection runs assert nothing was removed (the fake has no delete) and no operation was repeated (SC-006)
- [X] T023 [US3] In `scr/handlers/save_intake.py` add, before `accept_save` and before any ERP access: owner check (`caller_identity` and `uploaded_by` equal the reservation's, else `RESERVATION_FORBIDDEN` 403), fingerprint check (`validation.fingerprint` of the supplied customer, `po_number` and `len(lines)` equals the stored fingerprint, else `RESERVATION_MISMATCH` 422); keep the order: 404, 403, 409 not ready, 422, then `accept_save` (409 used)
- [X] T024 [US3] In `scr/syteline/savewf.py` add the outcome classification from research.md R7: after a definite `ErpRejected` on a line, read back that line's CO line and job rows (as `Upload._proven_absent` does for one key, generalized for the line and job keys); both proven absent and every earlier line verified -> `partly_saved`; otherwise `needs_reconciliation` with `possibly_written`; uncertain or unreachable-after-write -> `needs_reconciliation`; header rejection with nothing written and proven absent -> `failed` (numbers consumed). The worker maps `partly_saved` to `finish_partly_saved`
- [X] T025 [US3] Extend `scr/handlers/sweeper.py`: re-drive `save_accepted` reservations older than `SWEEPER_REDRIVE_AFTER_SECONDS` by invoking the worker with `{"reservation_id": ...}` (never `saving`, `needs_reconciliation` or `partly_saved`); park `saving` reservations older than `WORKER_TIMEOUT_SECONDS` plus 30 s as `needs_reconciliation` with `STALE_RUNNING`; the existing upload sweep is unchanged. Add cases to `tests/unit/test_worker_sweeper.py`
- [X] T026 [US3] Add `numbers_report(reservation)` to `scr/syteline/reservations.py` returning the `numbers` view from data-model.md (`usage` is `reserved`, `used` when the line's result is `saved`, or `consumed_unused` when the reservation is `failed` or the line was not saved), treating a `reserving` item older than the function timeout as `failed` with the numbers recorded so far consumed (research.md R5); unit tests in `tests/unit/test_reservations_store.py`

**Checkpoint**: safety rules hold under injected failures. Run `python -m pytest tests -q`.

---

## Phase 6: User Story 4 - Look up what happened (Priority: P3)

**Goal**: `GET /job-reservations/{id}` explains any reservation and its save.

**Independent Test**: reserve, save, query: state, numbers with usage and per-line results match what was saved; an unsaved reservation shows `reserved` only.

- [X] T027 [P] [US4] Add status cases to `tests/contract/test_reservation_routes.py` per `contracts/api.md`: 200 for `reserved`, `saved`, `partly_saved`, `needs_reconciliation` and `failed` with the documented fields (`lines` only after a save was accepted, `possibly_written` only on `needs_reconciliation`, `failure` when present), 404 for an unknown id, and that no response contains credentials, the full request payload or the caller ARN (SC-007)
- [X] T028 [US4] Create `scr/handlers/reservation_status.py` (`handler`) following `handlers/status.py`: build the view from the stored reservation using `numbers_report` and the saved `results`; never return `payload_hash`, `fingerprint`, `caller_identity` or the stored request; log with `log_event("reservation_status", ...)`
- [X] T029 [US4] Add a `ReservationStatusFunction` (read-only `DynamoDBReadPolicy` on `JobsTable`, `Timeout: 10`) with a `GET /job-reservations/{id}` event on `OrderApi` to `scr/template.yaml`; extend `tests/unit/test_template_reservation.py` (read-only policy, route present)

**Checkpoint**: all four stories work independently and together.

---

## Phase 7: Polish and cross-cutting

- [X] T030 [P] Write `docs/RESERVATIONS.md`: the two-stage flow, how to call each route, the state table, what "consumed but unused" means and how a person accounts for gaps, how to resolve `partly_saved` and `needs_reconciliation` by hand in SyteLine, that it exists in the VPC version only, and the UNCONFIRMED names to verify first; link it from `README.md` and `docs/DEPLOYMENT.md`
- [X] T031 [P] Add the three routes and the new error codes to `specs/004-job-number-workflow/contracts/api.md` only if implementation changed them; otherwise confirm the contract matches the code and note it in the PR description
- [X] T032 [P] Extend `tests/unit/test_no_secrets.py` and `tests/unit/test_no_sql.py` so the new modules (`reserve.py`, `savewf.py`, `reservations.py`, the three handlers) are included in their file scans
- [X] T033 Run the whole suite (`python -m pytest tests -q`; the 12 failures and 2 errors in `tests/unit/test_transport.py` that exist before this feature are unrelated and must not grow), `sam validate --lint -t scr/template.yaml`, and cfn-lint on `scr/template.yaml`; fix anything new
- [X] T034 Walk through `quickstart.md` sections 1 to 3 and tick each expected outcome; section 4 (live walkthrough) is for a test configuration only, after the read-only discovery task confirms the `UNCONFIRMED` names in `scr/syteline/idos.py`

---

## Dependencies and order

- Phase 1 and 2 first; T003 before T004, T011, T018 to T020, T026; T005 before T006 tests and T018; T007 before T012 and T023.
- US1 (T008-T014) has no dependency on later stories: it is the MVP.
- US2 (T015-T021) needs US1's reservation records and handler pattern, and Foundational T005/T006.
- US3 (T022-T026) refines US2's `save_intake.py`, `savewf.py` and the worker, so it follows US2; T025 and T026 can run in parallel with T023/T024 (different files).
- US4 (T027-T029) needs T026 and can run after US1; it does not depend on US2 except to show `lines`.
- Polish after the stories it documents or checks.

## Parallel examples

- After T003: T004, T005, T006, T007 together (different files).
- US1: T008, T009, T014 together (tests), then T010 -> T011 -> T012 -> T013.
- US3: T022 together with T025; T023 and T024 are in different files from T025 and T026.

## Implementation strategy

- **MVP**: Phases 1, 2 and US1 (T001-T014). A caller can get CO and job numbers from the ERP, safely, with nothing else written.
- **Then**: US2 makes the numbers useful (the save), US3 makes it safe to use in production, US4 makes it explainable. Do not enable live writes for any of it until the discovery task confirms the UNCONFIRMED names.
