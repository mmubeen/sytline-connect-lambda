---

description: "Task list for SyteLine ERP Integration (revised: all database access via the web service, FR-018)"
---

# Tasks: SyteLine ERP Integration

**Input**: Design documents from `/specs/001-syteline-erp-integration/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/api.md, contracts/lambda-actions.md, quickstart.md

**Tests**: Included (plan.md specifies pytest with fakes; the instruction sheet requires failure-handling validation).

**Organization**: Grouped by user story. Paths are relative to the repo root.

**Status key**: `[X]` = already built and still valid under FR-018. Unchecked = new work, or a rework of code built before FR-018 (it used a direct SQL connection for customer lookup, number allocation and notes, which is now forbidden).

**Hard rules**: FR-018 — no direct database (SQL) connection, driver, setting or credential anywhere. FR-016 — no task may write to the live ERP; tasks needing live access are marked BLOCKED and wait for network access or explicit ERP-owner approval.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1 = confirmed-order upload (P1), US2 = notes (P2), US3 = connectivity check (P3)

---

## Phase 1: Setup

- [X] T001 Create package skeleton `scr/syteline/__init__.py`, test dirs `tests/unit/`, `tests/contract/`, `tests/integration/` and `tests/conftest.py` (adds `scr/` and `tests/` to `sys.path`; autouse fixture turns live writes off)
- [X] T002 [P] Add `pytest` in `scr/requirements-dev.txt`
- [X] T003 [P] Create `scr/handlers/__init__.py`
- [X] T004 [P] Remove `python-tds` from `scr/requirements.txt` (leave zeep, lxml, requests, urllib3); rewrite `scr/.env.example` without any `SYTELINE_SQL_*` variable (keep `SYTELINE_WSDL`, `SYTELINE_USER`, `SYTELINE_PASS`, `SYTELINE_CONFIG`, `SYTELINE_SECRET_ARN`, `SYTELINE_DEFAULT_WHSE`, `SYTELINE_DEFAULT_ORDTYPE`, `SYTELINE_LIVE_WRITES=false`, `JOBS_TABLE`, `WORKER_FUNCTION_NAME`) and document the optional web-service name overrides read by `idos.py` (`SYTELINE_CUSTOMER_IDO` and the method-IDO overrides listed in T008)

---

## Phase 2: Foundational (blocks all user stories)

- [X] T005 Config loading in `scr/syteline/config.py` (env vars, optional Secrets Manager override, default config `CCT`, `skip_secret` for dry run, default warehouse/order type, `live_writes_enabled()` default false, `jobs_table()`, `worker_function_name()`)
- [X] T006 Remove the SQL settings from `scr/syteline/config.py` (`SYTELINE_SQL_HOST/PORT/DB/USER/PASS`, `sql_configured()`); update `tests/unit/test_config.py`: delete the SQL port tests and the SQL-write-refusal test, add a test that `load_config()` returns no key starting with `SYTELINE_SQL`
- [X] T007 [P] Error codes, exceptions (`ErpRejected`, `ErpUncertain`, `ErpUnreachable`, `LiveWritesDisabled`) and response helpers in `scr/syteline/errors.py`
- [X] T008 Create `scr/syteline/idos.py`, the single place for web-service names that are not yet confirmed (research.md R4): constants `CO_IDO="SLCos"`, `LINE_IDO="CCT_Coitems"`, `JOB_IDO="CCTJobs"`, `NOTE_IDO="SpecificNotes"`, `OBJECT_NOTE_IDO="ObjectNotes"`; `CUSTOMER_IDO` (name unconfirmed, overridable by env `SYTELINE_CUSTOMER_IDO`); method descriptors `NEXT_CO=(ido, "NextCoSp")`, `NEXT_JOB=(ido, "NextWOSp")`, `CREATE_NOTE=(ido, "CreateSpecificNoteSp")` (IDO names unconfirmed, overridable by env); `quote(value)` that doubles single quotes; `filter_eq(prop, value)` returning `"<prop> = '<escaped>'"`; add `tests/unit/test_idos.py` (an apostrophe or `'; DROP ...` input stays one escaped literal)
- [X] T009 Add `invoke(ido, method, params)` to `SoapIdoClient` in `scr/syteline/client.py` (web-service method call, SOAP operation name in `OPS`, unconfirmed): raises `LiveWritesDisabled` unless live writes are enabled (every call may write), `ErpRejected` on a SOAP fault, `ErpUncertain` on a transport failure after the request may have been sent, `ErpUnreachable` on a connect failure; returns the method's output values as a list of strings; no `delete` exists; extend `tests/unit/test_resilience.py` with these cases (insert behavior is already covered)
- [X] T010 [P] Rewrite `tests/fakes.py`: remove `FakeSql`; `FakeSoap` gains `invoke` (`NextCoSp`/`NextWOSp` counters, `CreateSpecificNoteSp` stores a note, gated by live writes, scripted `reject`/`timeout_before`/`timeout_after`/`unreachable` per call) and collection `load` for the customer collection (rows with name, address, `cust_num`, `cust_seq`), `SLCos`/`CCTJobs` `RowPointer`, `SpecificNotes` and `ObjectNotes` with `RefRowPointer` and `CreateDate >=` filters; keep `FakeJobsTable` and `FakeLambda`; there is still no delete operation
- [X] T011 DynamoDB job store and state machine in `scr/syteline/jobs.py` (atomic create + `request_ref` index, claim `accepted→running`, terminal transitions, progress, sweeper queries; nothing leaves `needs_reconciliation`)
- [X] T012 [P] Unit tests `tests/unit/test_jobs.py`

**Checkpoint**: config, names, client (read, insert, invoke), job store and fakes ready; no SQL anywhere.

---

## Phase 3: User Story 1 - Confirmed-order upload (P1) 🎯 MVP

**Goal**: `POST /confirmed-orders` durably accepts a confirmed order and returns a upload ID; a worker uploads it through the web service; `GET /upload-jobs/{id}` reports the state; uncertainty never triggers deletes or automatic retries.

**Independent Test**: With fakes, submit a valid order and see `accepted`, then `succeeded` with CO and per-line job numbers after the worker runs; every failure case in quickstart step 4 ends as specified. (Live check only with explicit ERP-owner approval.)

### Gate (run first; blocked until plant network access)

- [ ] T013 [US1] GATE, BLOCKED (needs network access to the plant; read-only calls only): discover from the WSDL and read-only `load` calls (1) the real SOAP operation names and payload shapes, (2) that `NextCoSp`, `NextWOSp` and `CreateSpecificNoteSp` exist as callable methods and on which IDOs, (3) the customer-address collection name and property names, (4) the `CCT_Coitems` and `CCTJobs` required properties; record results under "Resolved open items" in `specs/001-syteline-erp-integration/research.md` and correct `scr/syteline/idos.py`, `scr/syteline/client.py` and `scr/syteline/pipeline.py`; if a method is not callable, stop and revisit the numbering decision with the user; this task GATES T024, T025 and T037 (do not start those until it has run; if it cannot run, build only against fakes and mark the results unverified)

### Tests

- [X] T014 [P] [US1] Unit tests `tests/unit/test_validation.py` for the order request rules (constraints from data-model.md: `request_ref` "1–128", `confirmed` must be `true`, `uploaded_by` "required, non-empty", `customer.name` required, no customer number, `lines[]` "≥1", `quantity` "> 0", `price` "≥ 0")
- [X] T015 [P] [US1] Rewrite `tests/unit/test_resolver.py` for the web-service customer lookup using `FakeSoap`: exactly one match returns `{cust_num, cust_seq}`; none → `CUSTOMER_NOT_FOUND`; more than one distinct customer → `CUSTOMER_AMBIGUOUS`; only the bill-to row is considered; the load filter is a first-word prefix of the normalized name (plus postal code when supplied), capped at 50 rows (a full cap → `CUSTOMER_AMBIGUOUS`); a stored name that differs only in spacing or case still matches after normalization in code; an apostrophe or injection string in the name stays one escaped literal in the filter; the lookup never calls `insert` or `invoke` and works with live writes off
- [X] T016 [P] [US1] Rewrite `tests/unit/test_pipeline.py` using `FakeSoap`: call order is customer lookup → `invoke` `NextCoSp` → insert `SLCos` → per line (`invoke` `NextWOSp` → insert `CCT_Coitems` → insert `CCTJobs`) → read-back; success only when read-back matches every row; request values override the default warehouse/order type; **live writes off → stops after the customer lookup with `LIVE_WRITES_DISABLED` and no `invoke` or `insert` was attempted**; customer failures are definite `failed` with no number allocated; time-budget cases from the previous version stay
- [X] T017 [P] [US1] Rewrite `tests/unit/test_uncertainty.py` for the classification in research.md R6 using `FakeSoap` scenarios: first insert rejected + read-back proves absence → `failed`; same rejection with read-back failing → `needs_reconciliation`; any timeout after an insert (row stored or not) → `needs_reconciliation` and never `failed`; later-line timeout lists CO, lines and jobs; rejection or unreachable after earlier rows → `needs_reconciliation`; read-back failure or mismatch → `needs_reconciliation`; **number-allocation `invoke` failing before any row exists → `failed`, after a row exists → `needs_reconciliation`**; assert no delete exists and no step is attempted twice
- [X] T018 [P] [US1] Contract tests `tests/contract/test_intake.py` per contracts/api.md (202, 200 duplicate, 409, 400 with all problems, caller identity, async worker invoke, invoke failure leaves `accepted`, 503 when the store is down)
- [X] T019 [P] [US1] Contract tests `tests/contract/test_status.py` per contracts/api.md
- [X] T020 [P] [US1] Update `tests/unit/test_worker_sweeper.py` to drop the SQL client (the worker factory returns `(soap, cfg)`); keep: claim only `accepted`, duplicate invocation never re-runs an upload, outcome mapping, unexpected exception after writes → `needs_reconciliation`, sweeper re-drives only old `accepted`, stale `running` is parked and never re-driven
- [X] T021 [P] [US1] Add `tests/unit/test_concurrency.py` (SC-004 with fakes): 20 concurrent intakes with distinct `request_ref` create 20 jobs, the worker processes them serially, and the 20 CO numbers and 20 job numbers are all distinct; document in the module docstring that SC-001 timing and real number uniqueness can only be fully verified in an approved live test

### Implementation

- [X] T022 [P] [US1] Add `tests/unit/test_logging.py` (FR-011): each of intake, status, worker, sweeper and the direct-invoke handler emits one structured log line per request containing the request type, the identifiers involved (`upload_id`, `request_ref`, CO number or record key where known), the outcome and the duration in ms, and never a password or token
- [X] T023 [P] [US1] `scr/syteline/validation.py` `validate_order(body)` and `payload_hash(body)`
- [X] T024 [P] [US1] Rewrite `scr/syteline/resolver.py` `resolve_customer(soap, customer)` to satisfy T015: read-only `soap.load(idos.CUSTOMER_IDO, ...)` filtered per the lookup rule in research.md R5 step 1 (bill-to row only; `name LIKE '<FIRSTWORD>%'` via `idos.quote`, postal code when supplied, cap 50), then compare the full normalized name and address fields in code and require exactly one distinct customer; keep the docstring stating the matching rule is an assumption to align with the PO OCR resolver owner (research.md open item 3) (Built and tested against fakes only; the web-service names are unverified until T013.)
- [X] T025 [US1] Rework `scr/syteline/pipeline.py` to satisfy T016 and T017: `run_upload(request, soap, cfg, remaining_ms, on_progress)` (no SQL client argument); allocate numbers with `soap.invoke(*idos.NEXT_CO, ...)` and `soap.invoke(*idos.NEXT_JOB, ...)`; use the names from `idos.py`; keep the live-writes check after the customer lookup and before any `invoke`, the possibly-written bookkeeping before each send, the classification and the read-back verification; delete `NEXT_CO_SQL`/`NEXT_JOB_SQL` (Built and tested against fakes only; the web-service names are unverified until T013.)
- [X] T026 [US1] `scr/handlers/intake.py` (POST /confirmed-orders)
- [X] T027 [P] [US1] `scr/handlers/status.py` (GET /upload-jobs/{id})
- [X] T028 [US1] Update `scr/handlers/worker.py`: remove the SQL client and its `close()`; `_clients_factory` returns `(soap, cfg)`; call the new `pipeline.run_upload` signature; behavior otherwise unchanged
- [X] T029 [P] [US1] `scr/handlers/sweeper.py`
- [X] T030 [US1] Retire the superseded design: `orders.py`, `idempotency.py`, their tests and the `create_order` direct action are removed
- [X] T031 [US1] Rename the internal tracking ID to match the contract (spec Upload ID): `job_id` → `upload_id` and key prefix `JOB#` → `UPLOAD#` in `scr/syteline/jobs.py`, `scr/handlers/intake.py`, `status.py`, `worker.py`, `sweeper.py`, the API responses in contracts/api.md terms, and the tests that use them (`tests/unit/test_jobs.py`, `tests/contract/test_intake.py`, `tests/contract/test_status.py`, `tests/unit/test_worker_sweeper.py`, `tests/unit/test_no_secrets.py`, `tests/integration/test_upload_live.py`); "job" then refers only to SyteLine production jobs
- [X] T032 [US1] Update `scr/template.yaml`: remove the `SqlHost`, `SqlPort` and `SqlDatabase` parameters and every `SYTELINE_SQL_*` environment variable; update the parameter description of `SytelineSecretArn` (holds only the ERP password); add VPC endpoints for the worker's VPC (DynamoDB gateway endpoint and Secrets Manager interface endpoint, with route-table and security-group parameters) or document an equivalent NAT path as a deploy prerequisite; keep everything else; `cfn-lint scr/template.yaml` must pass
- [X] T033 [US1] `tests/integration/test_upload_live.py`, skipped unless `SYTELINE_LIVE=1` **and** `LIVE_APPROVAL_REF` are set, single approved order only
- [X] T034 [P] [US1] Rewrite `tests/integration/test_readonly_checks.py`: skipped unless `SYTELINE_READONLY_CHECKS=1`; asserts live writes are off, DNS and TCP to the web-service port, a login, and one read-only `load` on the customer collection; no `insert`, no `invoke`

**Checkpoint**: US1 works end to end against fakes with no SQL; live use waits on T013 and ERP-owner approval.

---

## Phase 4: User Story 2 - Notes on ERP records (P2)

**Goal**: `add_note` and `get_notes` on CO and job records through the web service, with writes gated.

**Independent Test**: With `FakeSoap`, add a note and read it back; with live writes off, `add_note` returns `LIVE_WRITES_DISABLED` and writes nothing.

### Tests

- [X] T035 [P] [US2] Rewrite `tests/contract/test_notes.py` per contracts/lambda-actions.md using `FakeSoap`: add then get round trip (internal and external, non-ASCII, long text, `since` filter), `NOT_FOUND` writes nothing, `VALIDATION`, `LIVE_WRITES_DISABLED` for `add_note` only (`get_notes` still works)
- [X] T036 [P] [US2] Rewrite `tests/unit/test_notes.py`: `add_note` first `load`s the record's `RowPointer` (CO from `SLCos`, job from `CCTJobs`, filter via `idos.filter_eq`), then `invoke`s `CreateSpecificNoteSp` with `ObjectName`, `RowPointer`, `NoteDesc`, `NoteText`, `InternalFlag` (1 internal / 0 external); `get_notes` reads `ObjectNotes` by `RefRowPointer` and `SpecificNotes` by `SpecificNoteToken`; an uncertain `invoke` is reported as `UNCERTAIN_OUTCOME` and not retried

### Implementation

- [X] T037 [US2] Rewrite `scr/syteline/notes.py` to satisfy T035 and T036 using `SoapIdoClient.load`/`invoke` and `idos.py` only (no SQL); response shapes per contracts/lambda-actions.md (Built and tested against fakes only; the web-service names are unverified until T013.)
- [X] T038 [US2] Update `scr/lambda_function.py`: replace the SQL factory with a SOAP client factory (`_client_factory`), pass it to the notes functions, map `LiveWritesDisabled` to `LIVE_WRITES_DISABLED` and `ErpUncertain` to `UNCERTAIN_OUTCOME`
- [X] T039 [US2] `tests/integration/test_notes_live.py` with the same double gate (`SYTELINE_LIVE=1` and `LIVE_APPROVAL_REF`)

**Checkpoint**: US1 + US2 work.

---

## Phase 5: User Story 3 - Connectivity check (P3)

**Goal**: The existing staged health check and dry run keep working unchanged; there is no SQL stage.

**Independent Test**: `{}`, `{"action":"health"}` and `{"dry_run":true}` keep their result shape; the dry run sends no credentials and logs in to nothing.

- [X] T040 [P] [US3] Contract test `tests/contract/test_health.py`: default action is health, dry run needs only the WSDL, stage names and order, stops at the first failure, unknown action error
- [X] T041 [US3] Stage logic in `scr/syteline/health.py` (`run_health`) wired into the dispatcher
- [X] T042 [US3] Local-run entry in `scr/lambda_function.py` and README "Invoke" mention `action`
- [X] T043 [P] [US3] Update `tests/contract/test_health.py`: delete the two SQL-stage tests; add a test that the dry run never creates a session (`CreateSessionToken` is not called) and that `{"check_sql": true}` has no effect on the stage list
- [X] T044 [US3] Remove the opt-in `sql` stage, its `sql()` function and the `check_sql` handling from `scr/syteline/health.py`

**Checkpoint**: US3 verified (SC-007).

---

## Phase 6: Polish & Cross-Cutting

- [X] T045 [P] Rewrite `tests/unit/test_no_secrets.py` without SQL: scan responses and captured logs of the intake, status, worker, sweeper, health and notes handlers for the ERP password, and assert the SOAP client's error messages never contain it (FR-010)
- [X] T046 [P] Add `tests/unit/test_no_sql.py` (FR-018): no file under `scr/` imports a SQL client (`pytds`, `pyodbc`, `pymssql`), `scr/syteline/sql.py` does not exist, `scr/requirements.txt` lists no SQL driver, `load_config()` returns no `SYTELINE_SQL*` key, and `scr/template.yaml` and `scr/.env.example` contain no `SQL` setting; it runs after the removal tasks
- [X] T047 [P] Operator runbook `docs/reconciliation-runbook.md` for `needs_reconciliation` jobs
- [X] T048 [P] Update `README.md`, `DEPLOY_PORTAL.md` and `docs/reconciliation-runbook.md`: remove every SQL mention (config table, ports, layer contents, network requirements — the worker needs only HTTP port 80 to the plant), state FR-018, describe `idos.py` and the discovery task, and list the VPC endpoint (or NAT) prerequisite for DynamoDB and Secrets Manager
- [X] T049 [P] Rebuild `dist/function.zip` (`lambda_function.py`, `syteline/`, `handlers/`) and `dist/deps-layer.zip` without `python-tds` (cross-platform pip flags per DEPLOY_PORTAL.md)
- [X] T052 [P] SAM deployment setup (single stack, us-gov-east-1): `scr/samconfig.toml`, `scr/Makefile` (layer + function build without Docker), `scr/deploy.sh` and `scr/deploy.env.example`, the dependency layer and per-function `BuildMethod: makefile` in `scr/template.yaml`, `.gitignore` entries; `sam validate --lint` and `sam build` pass locally, and `deploy.sh` refuses missing settings and live writes without an approval reference
- [X] T053 [P] Document SAM CLI deployment in `docs/DEPLOYMENT.md` (resources, prerequisites, configuration, validate/build/deploy, verification, live-write procedure, troubleshooting, teardown) and link it from `README.md`
- [ ] T050 BLOCKED (needs deployed stack, VPN and plant access): run quickstart.md steps 2–4 (read-only network checks including method-callability discovery, API checks including an unapproved identity being denied, simulated failures) and record results in quickstart.md
- [ ] T051 BLOCKED (needs explicit written approval from the ERP owner for one named test, plus a reconciliation and cleanup plan): run quickstart.md step 5 (live ERP) and record the outcome

---

## Dependencies & Execution Order

- Phase 1 → Phase 2 → stories. Within Phase 2: T006 and T008 first; T009 needs T008; T010 needs T008 and T009.
- US1 (Phase 3) is the MVP. T015–T017, T020, T021 (tests) before T024, T025, T028; T024 needs T008 and T010; T025 needs T009 and T024; T028 needs T025; T032 any time after T006.
- US2 (Phase 4) needs T008, T009 and T010. T038 edits `scr/lambda_function.py`; T044 edits `scr/syteline/health.py`, so they do not conflict.
- US3 (Phase 5): T043 before T044.
- T013 (read-only discovery) is the GATE for T024, T025 and T037 and is BLOCKED on plant network access; its results may change `idos.py`, `client.py` and `pipeline.py`. If a numbering or notes method is not callable, stop and ask the user. Work that does not depend on the names (T004–T012, tests, T028–T032 rename/template, US3) can proceed meanwhile.
- The no-SQL guard test (T046) runs last among the code tasks, after T004, T006, T025, T028, T037, T038 and T044. T031 needs T026–T029 and updates the tests written for them.
- T050 and T051 are last and BLOCKED; T051 additionally requires approval.

### Parallel opportunities

- Setup: T004. Foundational: T007, T010, T012.
- US1 tests T015, T016, T017, T020, T021 and T022 together; T024 and T034 together.
- US2 tests T035, T036 together. US3: T043.
- Polish: T045, T048, T049 together.

## Open decisions (not covered by any task)

These analysis findings still need a decision from the user; no task implements them until decided: **I1** (retry path for a `failed` job with the same `request_ref`), **I2** (a way to close a `needs_reconciliation` job), **U1** (`SLCos` fields taken from the customer match), **U2** (source and requiredness of the teardown value). Run `/speckit-clarify` for them.

## Implementation Strategy

1. **MVP**: Phases 1–2, then US1 against fakes (T015–T034). Stop and validate quickstart step 1 offline. Nothing in this MVP touches the live ERP.
2. Add US2 notes, then US3 cleanup and Polish.
3. T013, T050 and T051 happen only with network access and, for T051, ERP-owner approval; expect `idos.py`/`client.py`/`pipeline.py` corrections from T013.
