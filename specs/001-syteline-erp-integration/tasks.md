---

description: "Task list for SyteLine ERP Integration (revised after clarification against the instruction sheet)"
---

# Tasks: SyteLine ERP Integration

**Input**: Design documents from `/specs/001-syteline-erp-integration/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/api.md, contracts/lambda-actions.md, quickstart.md

**Tests**: Included (plan.md specifies pytest with fakes; the instruction sheet requires failure-handling validation).

**Organization**: Grouped by user story. Paths are relative to the repo root.

**Status key**: `[X]` = already delivered by the first implementation pass and still valid. Everything unchecked is new or a rework of code written against the superseded design (delete-based compensation, IDO numbering, synchronous `create_order`).

**Hard rule (FR-016)**: no task may write to the live ERP. Tasks needing live access are marked BLOCKED and wait for explicit approval from the ERP owner or for network access.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1 = confirmed-order upload (P1), US2 = notes (P2), US3 = connectivity check (P3)

---

## Phase 1: Setup

- [X] T001 Create package skeleton `scr/syteline/__init__.py`, test dirs `tests/unit/`, `tests/contract/`, `tests/integration/` and `tests/conftest.py` that adds `scr/` and `tests/` to `sys.path`
- [X] T002 [P] Add `pytest` in `scr/requirements-dev.txt`
- [X] T003 [P] Add `python-tds` to `scr/requirements.txt`; add `scr/handlers/__init__.py`
- [X] T004 [P] Rewrite `scr/.env.example`: keep `SYTELINE_WSDL`, `SYTELINE_USER`, `SYTELINE_PASS`, `SYTELINE_CONFIG`, `SYTELINE_SECRET_ARN`, `SYTELINE_DEFAULT_WHSE`, `SYTELINE_DEFAULT_ORDTYPE`; add `SYTELINE_SQL_HOST`, `SYTELINE_SQL_PORT` (no default; comment "verify with Chromalloy IT, do not assume 1433"), `SYTELINE_SQL_DB=cct_App`, `SYTELINE_SQL_USER`, `SYTELINE_SQL_PASS`, `SYTELINE_LIVE_WRITES=false`, `JOBS_TABLE`, `WORKER_FUNCTION_NAME`; remove `IDEMPOTENCY_TABLE`

---

## Phase 2: Foundational (blocks all user stories)

- [X] T005 Config loading in `scr/syteline/config.py` (env vars, optional Secrets Manager override, default config `CCT`, `skip_secret` for dry run, `SYTELINE_DEFAULT_WHSE`/`SYTELINE_DEFAULT_ORDTYPE`) with `tests/unit/test_config.py`
- [X] T006 Extend `scr/syteline/config.py`: add `SYTELINE_SQL_HOST`, `SYTELINE_SQL_PORT` (required when SQL is used; no default value), `SYTELINE_SQL_DB`, `SYTELINE_SQL_USER`, `SYTELINE_SQL_PASS` (these two overridable from the secret), `live_writes_enabled()` returning true only when `SYTELINE_LIVE_WRITES` is exactly `true`/`1`/`yes` (default false), `JOBS_TABLE`, `WORKER_FUNCTION_NAME`; extend `tests/unit/test_config.py` (live-writes default false; SQL port has no default; secret never read when `skip_secret`)
- [X] T007 [P] Extend `scr/syteline/errors.py` with the codes in contracts/api.md and contracts/lambda-actions.md: `CUSTOMER_NOT_FOUND`, `CUSTOMER_AMBIGUOUS`, `LIVE_WRITES_DISABLED`, `READBACK_MISMATCH`, `UNCERTAIN_OUTCOME`, `STALE_RUNNING`, `REQUEST_REF_CONFLICT`; remove `CLEANUP_FAILED`, `TIMEOUT_RISK`, `IN_PROGRESS`; add exception classes `ErpRejected` (definite: ERP refused the row), `ErpUncertain` (timeout/ambiguous after a possible write), `LiveWritesDisabled`
- [X] T008 Create `scr/syteline/sql.py`: connection factory using `python-tds` from config (never opens a connection at import or in dry run); `query(sql, params)` read-only helper; `call_proc(name, params, outputs)` helper that first calls `config.live_writes_enabled()` and raises `LiveWritesDisabled` if false; never logs the password or full connection string
- [X] T009 [P] Rewrite `tests/fakes.py`: `FakeSql` (scripted `custaddr` rows, `NextCoSp`/`NextWOSp` counters, records allocations, raises on write when live writes are off), `FakeSoap` (in-memory IDO tables `SLCos`/`CCT_Coitems`/`CCTJobs`; scenarios: accept, row-level rejection inside an HTTP 200 diffgram, timeout after the row was stored, timeout before it was stored, read-back mismatch; **no delete operation**), `FakeJobsTable` (DynamoDB get/put/update/transact_write with conditions), `FakeLambda` (records async invokes)
- [X] T010 Rework `scr/syteline/client.py` (SOAP IDO, keep session handling and one re-login on expired session): `load(ido, properties, filter)`; `insert(ido, values)` that parses the response for row-level errors inside HTTP 200 and raises `ErpRejected` (definite) with the ERP text, or `ErpUncertain` on timeout/transport error after the request was sent; `read_back(ido, filter, properties)`; remove `delete`, `next_number`, `invoke`, `NUMBER_METHODS`; insert raises `LiveWritesDisabled` when live writes are off; SOAP stays HTTP/port 80 (no TLS assumptions); update `tests/unit/test_resilience.py` accordingly (re-login once, transport error → `ErpUncertain` on insert / `ErpUnreachable` on load, diffgram rejection parsing)
- [X] T011 Create `scr/syteline/jobs.py` (DynamoDB, table `JOBS_TABLE`): `create_job(request, payload_hash, uploaded_by, caller_identity)` writing the `JOB#<id>` item and `REF#<request_ref>` index atomically via TransactWriteItems with `attribute_not_exists`; `get_job(job_id)`; `find_by_ref(ref)`; transitions with conditional updates exactly as in data-model.md: `accepted→running` (claim), `running→succeeded|failed|needs_reconciliation`; `list_accepted_older_than(seconds)`; `list_running_older_than(seconds)`; no transition ever leaves `needs_reconciliation` automatically; `ttl` 90 days after a terminal state; ints only (no floats) for DynamoDB
- [X] T012 [P] Unit tests `tests/unit/test_jobs.py`: atomic create + ref index; same ref and same hash returns the existing job; same ref with a different hash is a conflict; only `accepted` can be claimed; second claim of the same job fails; `needs_reconciliation` is never returned by `list_accepted_older_than`; stale `running` listed for parking

**Checkpoint**: config, SQL helper, SOAP client, job store and fakes ready.

---

## Phase 3: User Story 1 - Confirmed-order upload (P1) 🎯 MVP

**Goal**: `POST /confirmed-orders` durably accepts a confirmed order and returns a job ID; a worker uploads it through the documented pipeline; `GET /upload-jobs/{id}` reports the state; uncertainty never triggers deletes or automatic retries.

**Independent Test**: With fakes, submit a valid order and see `accepted`, then `succeeded` with CO and per-line job numbers after the worker runs; every failure case in quickstart step 4 ends as specified. (Live check only with explicit ERP-owner approval.)

### Tests

- [X] T013 [P] [US1] Unit tests `tests/unit/test_validation.py` (rewrite) for `scr/syteline/validation.py` per data-model.md ConfirmedOrderRequest: `request_ref` "1–128" chars; `confirmed` must be `true`; `uploaded_by` required non-empty; `customer` is an object with required `name`; a `customer_number`/`cust_num` field is rejected ("Never a customer number"); `lines` ≥1; `quantity` > 0; `uom` required; dates `YYYY-MM-DD`; `price` ≥ 0; `teardown` optional string; all problems reported together
- [X] T014 [P] [US1] Unit tests `tests/unit/test_resolver.py` for `scr/syteline/resolver.py`: exactly one `custaddr` match returns `{cust_num, cust_seq}`; none → `CUSTOMER_NOT_FOUND`; more than one → `CUSTOMER_AMBIGUOUS`; matching is exact on normalized name and (when supplied) address, using parameterized queries (a name with a quote character does not break the query); read-only (no SQL write path used)
- [X] T015 [P] [US1] Unit tests `tests/unit/test_pipeline.py` for `scr/syteline/pipeline.py` using the fakes: call order is customer resolve → `NextCoSp` → insert `SLCos` → per line (`NextWOSp` → insert `CCT_Coitems` with teardown in `UserDefinedFields` → insert `CCTJobs`) → read-back; success only when read-back matches every row; **live writes off → stops after customer resolution with `LIVE_WRITES_DISABLED` and `FakeSql` shows zero allocations**; customer failure → definite `failed`, no number allocated
- [X] T016 [P] [US1] Unit tests `tests/unit/test_uncertainty.py` for the classification in research.md R6 (use `FakeSoap` scenarios): row-level rejection in HTTP 200 on the first insert + read-back proves absence → `failed` (retriable); the same rejection with read-back failing → `needs_reconciliation`; timeout after an insert was sent with read-back showing the row → `needs_reconciliation` with `possibly_written`; timeout on a later line → `needs_reconciliation` listing CO, lines and jobs attempted; read-back mismatch → `needs_reconciliation` (`READBACK_MISMATCH`); assert in every case that no delete call exists and no second attempt is made
- [X] T017 [P] [US1] Contract tests `tests/contract/test_intake.py` for `scr/handlers/intake.py` per contracts/api.md: 202 + `accepted`; 200 `duplicate:true` for same ref and body; 409 `REQUEST_REF_CONFLICT`; 400 listing all validation errors and nothing stored; caller identity from `requestContext.identity.userArn` and `uploaded_by` stored; worker invoked asynchronously (`InvocationType=Event`); if the invoke call fails the job stays `accepted` and the response is still 202
- [X] T018 [P] [US1] Contract tests `tests/contract/test_status.py` for `scr/handlers/status.py`: 200 for each of the five states with the right fields (`co_num`/`jobs` only when `succeeded`, `failure` when failed, `possibly_written` when `needs_reconciliation`); 404 for an unknown id
- [X] T019 [P] [US1] Unit tests `tests/unit/test_worker_sweeper.py`: worker claims `accepted→running` and ignores a job that is not `accepted` (a duplicate invocation never re-runs an upload); worker maps pipeline outcomes to `succeeded`/`failed`/`needs_reconciliation`; sweeper re-invokes only `accepted` jobs older than a threshold; sweeper moves `running` older than the worker timeout to `needs_reconciliation` (`STALE_RUNNING`) and never re-invokes it

### Implementation

- [X] T020 [P] [US1] Rewrite `scr/syteline/validation.py` `validate_order(body)` to satisfy T013 (constraints quoted from data-model.md: `request_ref` "1–128", `confirmed` must be `true`, `uploaded_by` "required, non-empty", `customer.name` required, `lines[]` "≥1", `quantity` "> 0", `price` "≥ 0"); also export `payload_hash(body)` (SHA-256 of canonical JSON)
- [X] T021 [P] [US1] Create `scr/syteline/resolver.py` `resolve_customer(sql, customer)` satisfying T014; add a module docstring stating the matching rule is an assumption to be aligned with the PO OCR resolver owner (research.md open item 1)
- [X] T022 [US1] Create `scr/syteline/pipeline.py` `run_upload(job, sql, soap, cfg)` implementing research.md R5 and the R6 classification, satisfying T015 and T016: returns an outcome object (`succeeded` with `co_num` and `lines[{line,job}]`, `failed` with code/step, or `needs_reconciliation` with `possibly_written[{kind,key}]`); checks `live_writes_enabled()` after the customer match and before any number allocation; writes `teardown` to `UserDefinedFields` per line; uses `SYTELINE_DEFAULT_WHSE`/`SYTELINE_DEFAULT_ORDTYPE` when the request omits `warehouse`/`order_type`; never calls any delete; never retries a step
- [X] T023 [US1] Create `scr/handlers/intake.py` `handler(event, context)` for API Gateway proxy events satisfying T017: parse body (400 on invalid JSON), validate, create job via `jobs.py`, async-invoke `WORKER_FUNCTION_NAME` with `InvocationType="Event"`, return 202/200/400/409 per contracts/api.md
- [X] T024 [P] [US1] Create `scr/handlers/status.py` `handler(event, context)` for `GET /upload-jobs/{id}` satisfying T018
- [X] T025 [US1] Create `scr/handlers/worker.py` `handler(event, context)` satisfying T019: claim `accepted→running`, build SQL and SOAP clients from config, call `pipeline.run_upload`, persist the outcome and `customer_match`/`erp_records`, `attempts += 1`; any unexpected exception after a possible write → `needs_reconciliation` (`UNCERTAIN_OUTCOME`), before any write → `failed`
- [X] T026 [P] [US1] Create `scr/handlers/sweeper.py` `handler(event, context)` satisfying T019 (thresholds from env with defaults: re-drive `accepted` older than 120 s; park `running` older than the worker timeout plus 30 s)
- [X] T027 [US1] Retire the superseded design: delete `scr/syteline/orders.py`, `scr/syteline/idempotency.py`, `tests/unit/test_orders.py`, `tests/unit/test_idempotency.py`, `tests/unit/test_errors.py`, `tests/contract/test_create_order.py`; remove the `create_order` action and its helpers from `scr/lambda_function.py` (leaving `health`, `add_note`, `get_notes`); update the module docstring of `scr/lambda_function.py`
- [X] T028 [US1] Update `scr/template.yaml`: private REST API (`EndpointConfiguration: PRIVATE`) with parameters for the `execute-api` VPC endpoint id and a resource policy limiting `aws:SourceVpce`; `POST /confirmed-orders` and `GET /upload-jobs/{id}` with `AuthorizationType: AWS_IAM`; functions `IntakeFunction`, `StatusFunction`, `WorkerFunction` (VPC config parameters, `ReservedConcurrentExecutions: 1`, timeout ≥ 60 s, `EventInvokeConfig` with `MaximumRetryAttempts: 0`), `SweeperFunction` with an EventBridge schedule, the jobs table (`pk` string hash key, TTL attribute `ttl`), IAM (DynamoDB on the table, `lambda:InvokeFunction` on the worker, `secretsmanager:GetSecretValue`), and `SYTELINE_LIVE_WRITES: "false"` as the default environment value
- [X] T029 [US1] Rewrite `tests/integration/test_create_order_live.py` into `tests/integration/test_upload_live.py`: skipped unless `SYTELINE_LIVE=1` **and** `LIVE_APPROVAL_REF` (the ERP owner's approval reference) is set; module docstring states the approved single test order, customer, and the reconciliation and cleanup plan must be agreed first; it submits one order through the intake handler and polls status; no parallel or repeated submissions
- [X] T030 [P] [US1] Create read-only checks `tests/integration/test_readonly_checks.py`: skipped unless `SYTELINE_READONLY_CHECKS=1`; DNS resolution of the WSDL host, TCP to SOAP port and SQL port, and a read-only `SELECT TOP 1` from `custaddr`; no writes of any kind
- [ ] T031 [US1] BLOCKED (needs network access to the plant; read-only): discover the SOAP operation names and payload shapes from the WSDL and the `CCT_Coitems`/`CCTJobs` required properties using read-only calls only; record results under "Resolved open items" in `specs/001-syteline-erp-integration/research.md` and correct `scr/syteline/client.py` and `scr/syteline/pipeline.py` accordingly

**Checkpoint**: US1 works end to end against fakes; live use waits on T031 and ERP-owner approval.

---

## Phase 4: User Story 2 - Notes on ERP records (P2)

**Goal**: `add_note` and `get_notes` on CO and job records through SQL, with writes gated.

**Independent Test**: With `FakeSql`, add a note and read it back; with live writes off, `add_note` returns `LIVE_WRITES_DISABLED` and writes nothing.

### Tests

- [X] T032 [P] [US2] Contract tests `tests/contract/test_notes.py` (rewrite) per contracts/lambda-actions.md: add then get round trip (internal and external, non-ASCII, long text, `since` filter), `NOT_FOUND` writes nothing, `VALIDATION`, and `LIVE_WRITES_DISABLED` when the flag is off (also for `add_note` only; `get_notes` still works)
- [X] T033 [P] [US2] Unit tests `tests/unit/test_notes.py` (rewrite) with `FakeSql`: `CreateSpecificNoteSp` called with `ObjectName`, `RowPointer`, `NoteDesc`, `NoteText`, `InternalFlag` (1 internal / 0 external); record lookup uses parameterized queries; queries match `sql/NotesExampleQueries.txt` joins (`SpecificNotes` ↔ `ObjectNotes` on `SpecificNoteToken`, `RefRowPointer` = record `RowPointer`)

### Implementation

- [X] T034 [US2] Rewrite `scr/syteline/notes.py` to use `scr/syteline/sql.py`: resolve the record `RowPointer` from the `co` / `job` table by key (parameterized), call `CreateSpecificNoteSp` via `sql.call_proc` (gated by live writes), read notes with the joins from `sql/NotesExampleQueries.txt` (optionally `CreateDate >= since`), return the shapes in contracts/lambda-actions.md; internal flag read from `ObjectNotes.NoteFlag`
- [X] T035 [US2] Update the `add_note`/`get_notes` handlers in `scr/lambda_function.py` to build the SQL helper (no SOAP client) and map `LiveWritesDisabled` to the `LIVE_WRITES_DISABLED` error
- [X] T036 [US2] Rewrite `tests/integration/test_notes_live.py` with the same double gate as T029 (`SYTELINE_LIVE=1` and `LIVE_APPROVAL_REF`) and a note-cleanup plan in its docstring

**Checkpoint**: US1 + US2 work.

---

## Phase 5: User Story 3 - Connectivity check (P3)

**Goal**: The existing staged health check and dry run keep working; the SQL path is checked too when configured.

**Independent Test**: `{}`, `{"action":"health"}` and `{"dry_run":true}` keep their result shape; the dry run never opens SQL.

- [X] T037 [P] [US3] Contract test `tests/contract/test_health.py`: default action is health, dry run needs only the WSDL, stage names and order, stops at the first failure, unknown action error
- [X] T038 [US3] Move the stage logic into `scr/syteline/health.py` (`run_health`) preserving behavior (HTTP 401/403 passes in dry run) and wire it into the dispatcher in `scr/lambda_function.py`
- [X] T039 [US3] Keep the local-run entry in `scr/lambda_function.py` working and mention `action` in README.md "Invoke"
- [X] T040 [P] [US3] Tests in `tests/contract/test_health.py`: with SQL settings present, non-dry runs add a final `sql` stage (TCP connect to `SYTELINE_SQL_HOST:SYTELINE_SQL_PORT`); without SQL settings the stage list is unchanged; **dry run never calls the SQL factory** (assert the factory is not invoked)
- [X] T041 [US3] Add the optional `sql` TCP stage to `scr/syteline/health.py` (non-dry only; error message names the host and port, never credentials)

**Checkpoint**: US3 verified (SC-007).

---

## Phase 6: Polish & Cross-Cutting

- [X] T042 [P] Update `tests/unit/test_no_secrets.py`: scan responses and captured logs of the intake, status, worker, sweeper and health handlers (including SQL and SOAP password values) for secrets (FR-010)
- [X] T043 [P] Create `docs/reconciliation-runbook.md`: how an operator handles a `needs_reconciliation` job (read `possibly_written`, check the CO/lines/jobs in SyteLine by number, decide close as succeeded / failed, how a resubmission is authorized); referenced from the status contract
- [X] T044 [P] Update `README.md` and `DEPLOY_PORTAL.md`: private API, IAM invocation example, worker VPC and plant-network requirements (HTTP port 80, verified SQL port, plant DNS), the live-writes flag (default false) and approval rule, new environment variables, `pytest`, the layer now including `python-tds`, handlers packaged in `function.zip`
- [X] T045 [P] Rebuild `dist/function.zip` (`lambda_function.py`, `syteline/`, `handlers/`) and the dependency layer including `python-tds` (cross-platform pip flags per DEPLOY_PORTAL.md)
- [ ] T046 BLOCKED (needs deployed stack, VPN and plant access): run quickstart.md steps 2–4 (read-only network checks, API checks including an unapproved identity being denied, simulated failures) and record results in quickstart.md
- [ ] T047 BLOCKED (needs explicit written approval from the ERP owner for one named test, plus a reconciliation and cleanup plan): run quickstart.md step 5 (live ERP) and record the outcome

---

## Dependencies & Execution Order

- Phase 1 → Phase 2 → stories. Within Phase 2: T006 before T008/T010/T011; T007 before T010/T011; T009 can run any time after T006.
- US1 (Phase 3) is the MVP. T013–T019 (tests) before T020–T026; T022 needs T020, T021, T008, T010; T023 needs T011 and T020; T025 needs T022 and T011; T027 after T022–T026 pass; T028 after the handlers exist.
- US2 (Phase 4) needs T008 and T006; independent of US1 except sharing `scr/lambda_function.py` with T027 (do T027 first).
- US3 (Phase 5): T040/T041 need T006 and T008; the `[X]` items are done.
- T031 (read-only discovery) is BLOCKED on network access and can run after Phase 2; its results may change T010 and T022.
- T046 and T047 are last and BLOCKED; T047 additionally requires approval.

### Parallel opportunities

- Setup: T003, T004. Foundational: T007, T009, T012.
- US1 tests T013–T019 together; T020, T021, T024, T026 together.
- US2 tests T032, T033 together. US3: T037 (done), T040.
- Polish: T042–T045 together.

## Implementation Strategy

1. **MVP**: Phases 1–2, then US1 against fakes (T013–T030). Stop and validate quickstart step 1 offline. Nothing in this MVP touches the live ERP.
2. Add US2 notes, then US3's SQL stage and Polish.
3. T031 (read-only discovery), T046 and T047 happen only with network access and, for T047, ERP-owner approval; expect `client.py`/`pipeline.py` corrections from T031.
