# Tasks: Production-Like Mock Environment

**Input**: Design documents from `/specs/008-production-like-mock-env/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: requested by the spec (the suite is the feature). Pure-Python parts get in-process unit tests under `tests/unit/mock/` that run in the normal suite. Container-dependent tests live in `tests/mock_env/` and are skipped unless `MOCK_ENV=1`.

**Verification note (R12)**: Docker is not installed in the authoring environment. Tasks marked **[DOCKER]** can only be verified on a machine with Docker; the completion report must say which were and were not run.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no unfinished dependency)
- **[Story]**: US1 full intake run, US2 failure rehearsal, US3 production parity, US4 repeatable and safe

## Phase 1: Setup

- [X] T001 Create the layout from plan.md: `mock/` (with `edge/`, `erp/`, `idp/`, `driver/`, `sqlserver/`, `scenarios/`), `tests/mock_env/`, `tests/unit/mock/`, each Python package with `__init__.py`
- [X] T002 [P] Add `mock/requirements.txt` (moto[server], werkzeug, pyyaml, requests, boto3) and a `.gitignore` entry for `mock/.run/` (generated CA, keys, passwords)
- [X] T003 [P] Register the `mockenv` marker and the `MOCK_ENV=1` skip rule in `tests/mock_env/conftest.py` and `pytest.ini`/`conftest.py` without changing existing test behaviour (marker registered in `tests/conftest.py`; skip rule in `tests/mock_env/conftest.py`)
- [X] T004 [P] Add `docs/MOCK_ENVIRONMENT.md` skeleton with headings: Start, Scenarios, Faults, Fidelity gaps (FR-016), Troubleshooting (superseded by T040)

## Phase 2: Foundational (blocks all stories)

- [X] T005 Write failing unit tests in `tests/unit/mock/test_render_env.py`: from `src/template.yaml`, `render_env` returns the function environment (names and values), `Timeout`, `MemorySize`, function name `<stack>-fn`, the route table of `OrderApi`, and the IAM statements of `DirectFunction`, with `!Sub`, parameters and conditions resolved using `tests/cfn_eval.py`; optional blocks (`HasIdp`, `HasIdpKeys`, `HasApi`) follow the parameters given
- [X] T006 Implement `mock/render_env.py` to satisfy T005 (reuse `tests/cfn_eval.Evaluator`; raise on any unsupported intrinsic so a template change cannot pass silently)
- [X] T007 [P] Write failing unit tests in `tests/unit/mock/test_policy.py`: Allow/Deny evaluation with action and resource globs, explicit Deny wins, no match denies, AWS-worded AccessDenied text `User: <role> is not authorized to perform: <action> on resource: <arn>`
- [X] T008 [P] Implement `mock/edge/policy.py` to satisfy T007
- [X] T009 [P] Write failing unit tests in `tests/unit/mock/test_actions.py` and implement `mock/edge/actions.py`: map (service, operation, request) to (IAM action, resource ARN) for DynamoDB, Secrets Manager, S3, KMS decrypt-on-read, CloudFormation DescribeStacks, Lambda Invoke; unknown operations are denied and recorded as `unmapped`
- [X] T010 Define the data model in `mock/edge/models.py`: Call record, Fault, Scenario per data-model.md (call bodies redacted: no passwords, tokens or secret values), with unit tests in `tests/unit/mock/test_models.py` including the redaction rule
- [X] T011 Write failing unit tests in `tests/unit/mock/test_control_api.py` for every route in `contracts/control-api.md` (health, reset, scenarios, faults add/clear with unknown id -> 400 listing the catalogue, calls filter by service and `since`, loopback-only refusal), then implement the control API in `mock/edge/control.py`
- [X] T012 Implement the edge proxy in `mock/edge/app.py`: forward to Moto, record each call, check permission through `policy.py` using the statements from `render_env.py`, apply active faults, and handle Lambda `Invoke` (`Event` -> 202 and background run; `RequestResponse` waits; unknown target -> `ResourceNotFoundException`); unit-test routing and fault precedence in `tests/unit/mock/test_edge.py` with Moto and the runtime stubbed
- [X] T013 Implement the seeder `mock/edge/seed.py`: create the jobs table with the template's key schema and TTL attribute, the secret (ARN in the `aws-us-gov` partition, generated passwords, `SQL_CA_PEM`), IDP stack outputs, tracking table and S3 objects from a scenario; unit-test against Moto in-process in `tests/unit/mock/test_seed.py`
- [ ] T014 [P] Write `mock/compose.yaml`: services `moto`, `edge`, `erp`, `sqlserver`, `fn` (Lambda runtime image with the built package mounted read-only, `AWS_ENDPOINT_URL` set to the edge, `mem_limit` and `AWS_LAMBDA_FUNCTION_TIMEOUT` from `render_env.py`), `driver`; network `internal: true`; ports published to `127.0.0.1` only; health checks on every service **[DOCKER]**  **NOT RUN: needs Docker (compose.yaml written).**
- [ ] T015 Write `mock/Makefile` with `up` (build function with `sam build`, generate CA via `mock/sqlserver/certs.sh`, render env, `docker compose up -d --wait`), `down`, `reset`, `test`, `logs`; the generated secrets go to `mock/.run/` and are never echoed **[DOCKER]**  **NOT RUN: needs Docker (Makefile written; its guards are unit-tested).**

**Checkpoint**: policy, rendering, edge, control API and seeder pass in-process; `make -C mock up` reaches `ready`.

## Phase 3: User Story 1 - Whole intake against a mock production (P1) MVP

**Goal**: send the sample event and get the production outcome with every ERP and database action recorded.
**Independent Test**: `MOCK_ENV=1 pytest tests/mock_env/test_intake_end_to_end.py` passes on a fresh `make -C mock up`.

- [X] T016 [P] [US1] Write failing unit tests in `tests/unit/mock/test_erp_mock.py` for `mock/erp/app.py`: WSDL served; `LoadDataSet`, `SaveDataSet`, `CallMethod` answer in the shapes parsed by `src/syteline/client.py`; login check; row-level rejection returned inside HTTP 200; every request stored as a call record
- [X] T017 [US1] Implement `mock/erp/app.py` and `mock/erp/service.wsdl` to satisfy T016 (derive operation names and payloads from `src/syteline/client.py` and `idos.py`; confirm by running the real `SoapIdoClient` against the app in the same unit test) (found: zeep drops a bare list for an `ArrayOfString` parameter; see docs/MOCK_ENVIRONMENT.md)
- [ ] T018 [P] [US1] Write `mock/sqlserver/init.sql` (types `LongList`, `Infobar`, tables `custaddr`, `co`, stand-in `NextCoSp` and `NextWOSp` that allocate from a sequence table and return `@Key` and `@Infobar` in the shape `src/syteline/sqlserver.py` reads), `mock/sqlserver/certs.sh` (per-run CA and server certificate, SAN = compose host name) and `mock/sqlserver/mssql.conf` (TLS, `forceencryption=1`) **[DOCKER]**  **NOT RUN: needs Docker (SQL, certificate and TLS files written, never run on an engine).**
- [X] T019 [P] [US1] Build the IDP stub in `mock/idp/` : seeded stack outputs, tracking item and S3 objects plus the `LookupFunction` stub; add `tests/unit/mock/test_idp_seed.py` that reads every seeded scenario through the real `idp_sdk` (path from `IDP_REPO`; skip with a clear message if unset) to prove the shapes are right (every scenario read back through the real IDP SDK over the edge and Moto)
- [X] T020 [P] [US1] Create the first scenarios under `mock/scenarios/`: `passes-review` (synthetic document with every confidence above threshold), `already-processed`, `incomplete`, `needs-review` (shape of the `159112.pdf` case, synthetic values only, FR-015); each with `scenario.json`, `idp.json`, `erp.json`, `sql.sql`, `secrets.json`
- [X] T021 [US1] Implement scenario loading (`POST /scenario/{name}` in `mock/edge/control.py`): reset, seed Moto, load ERP rows, run `sql.sql`, return the event to send
- [X] T022 [US1] Implement the `fn` invoke path in the control API (`POST /invoke`, sync and async, timeout) and the Lambda `Event` shim to the runtime emulator in `mock/edge/app.py`
- [X] T023 [US1] Write `tests/mock_env/conftest.py`: readiness gate (message "mock environment not running: start it with `make -C mock up`" and no pass/fail when absent), control-API client fixture, reset-before-each-test fixture, address-allowlist guard (every address in the function env must be a compose name or loopback, FR-011) and an assertion that live writes are off (address guard + not-running exit; live writes are ON inside the mock, only for mock addresses)
- [ ] T024 [US1] Write `tests/mock_env/test_intake_end_to_end.py`: `passes-review` -> `reserved` then `save_accepted` and the ERP call record lists the exact writes; second send -> `already_processed` with no second write; `incomplete` -> `DOCUMENT_INCOMPLETE`; `needs-review` -> `DOCUMENT_NEEDS_REVIEW` with nothing written **[DOCKER]**  **NOT RUN: needs Docker (written).**
- [ ] T025 [US1] Run the stack and the suite; record outcomes and any mismatch between mock and function in `specs/008-production-like-mock-env/run-notes.md` **[DOCKER]**  **NOT RUN: needs Docker.**

**Checkpoint**: US1 deliverable and demonstrable alone.

## Phase 4: User Story 2 - Rehearse production failures (P1)

**Goal**: every known failure can be switched on and shows the operator-facing result.
**Independent Test**: `MOCK_ENV=1 pytest tests/mock_env/test_faults.py`.

- [X] T026 [US2] Write failing unit tests in `tests/unit/mock/test_faults.py` for the catalogue in `contracts/control-api.md`: each fault id exists, applies to its target only, honours `remaining`, clears on `DELETE /faults` and on reset, and `idp.kms_denied` produces the AWS-worded denial including `params.key_arn`
- [X] T027 [US2] Implement the fault catalogue in `mock/edge/faults.py` (IDP/AWS faults in the edge) and hooks for `erp.*` in `mock/erp/app.py`
- [X] T028 [P] [US2] Implement SQL faults in `mock/edge/sqlfaults.py`: stop/start the container (`sql.unreachable`), change the login (`sql.login_refused`), make the routine raise (`sql.routine_error`), delay (`sql.slow`); scripted through the Docker CLI **[DOCKER]** (done differently: a TCP front `edge/sqlproxy.py` + `edge/sqlcontrol.py`, no Docker CLI needed; unit-tested, container behaviour unverified)
- [X] T029 [P] [US2] Implement `lock.held` (write the ERP-lock item `LOCK#erp-writes` into the mock jobs table with a live lease) and `secrets.unavailable` in `mock/edge/faults.py`
- [ ] T030 [US2] Write `tests/mock_env/test_faults.py` with one test per fault: `idp.kms_denied` -> `IDP_UNAVAILABLE` and, with `"debug": true`, the detail names the key; `idp.lookup_denied`; `erp.unreachable` / `erp.slow` -> reported within the time budget and the job left for the sweep (`POST /tick/sweep` then completes it once the fault is cleared); `sql.*`; `lock.held` -> `deferred: true` then done after release; each asserts nothing was written to the mock ERP **[DOCKER]**  **NOT RUN: needs Docker (written).**
- [ ] T031 [US2] Add a test that clearing a fault restores normal behaviour with no leftover state (acceptance scenario 3) in `tests/mock_env/test_faults.py` **[DOCKER]**  **NOT RUN: needs Docker (written).**

**Checkpoint**: US1 and US2 pass independently.

## Phase 5: User Story 3 - Same entry points, settings and limits (P2)

**Goal**: the mock cannot be easier than production, and drift is caught.
**Independent Test**: `MOCK_ENV=1 pytest tests/mock_env/test_parity.py`.

- [X] T032 [P] [US3] Implement the schedule and API shim in `mock/driver/app.py`: sweep tick every `MOCK_SWEEP_SECONDS` (default 20) and on demand; HTTP request -> API Gateway proxy event for each route in the template (`POST /__mock__/invoke/api`); unit tests in `tests/unit/mock/test_driver.py`
- [X] T033 [P] [US3] Write `tests/unit/mock/test_parity_static.py` (runs in the normal suite): every environment name in the template is in the rendered env; every action in `src/lambda_function.py` (`ACTIONS`, `WORK_ACTIONS`, `document_event`) appears in `docs/MOCK_ENVIRONMENT.md`; every `OrderApi` route is in the shim table; the fault catalogue in code equals the one in `contracts/control-api.md`
- [ ] T034 [US3] Write `tests/mock_env/test_parity.py`: the container received the same env names and values as `render_env`; its timeout and memory equal the template's; a request longer than the timeout is stopped and reported as a timeout; a call outside the template's permissions (for example `s3:PutObject`) is denied; each API route reaches its handler and an unknown route gets 404 **[DOCKER]**  **NOT RUN: needs Docker (written).**
- [X] T035 [US3] Add a drift test that fails when the template gains an env name or route the mock does not know (simulate with a copy of the template in `tmp_path`) in `tests/unit/mock/test_parity_static.py`

## Phase 6: User Story 4 - Repeatable, safe, easy to reset (P3)

**Goal**: unattended, identical results, nothing leaves the machine.
**Independent Test**: run `make -C mock test` twice from reset; compare.

- [X] T036 [US4] Implement full reset in `mock/edge/control.py` (`POST /reset`): Moto reset and re-seed, SQL truncate and re-seed, ERP state and call records, faults, lock items; restart the `fn` container option; unit tests for the in-process parts in `tests/unit/mock/test_reset.py` (in-process parts)
- [ ] T037 [P] [US4] Write `tests/mock_env/test_isolation.py`: two consecutive full runs give identical outcomes and call records (ignoring timestamps and ids); from inside the `fn` container an outbound connection to a public address fails; `SYTELINE_LIVE_WRITES` is false; real credentials in the host environment are not present in any container env (`docker inspect`) **[DOCKER]**  **NOT RUN: needs Docker (written).**
- [X] T038 [US4] Add concurrent-run protection: `make -C mock up` refuses when another run holds `mock/.run/lock`, with a message naming it (edge case); add a readiness timeout that names the service that was not ready; test the message paths in `tests/unit/mock/test_make_guards.py`
- [ ] T039 [US4] Add `make -C mock test` that starts, waits, runs `MOCK_ENV=1 pytest tests/mock_env`, and returns pytest's exit code, finishing within the SC-002 limit **[DOCKER]**  **NOT RUN: needs Docker (written).**

## Phase 7: Polish

- [X] T040 [P] Complete `docs/MOCK_ENVIRONMENT.md`: how to start and reset, scenarios, the fault catalogue, and the **fidelity gaps** (stand-in numbering routines, key policies/SCPs/permission boundaries not modelled, compressed schedule, emulated x86 SQL Server on Apple silicon, Moto vs real AWS behaviours); link from `README.md` and `docs/TESTING.md`
- [X] T041 [P] Add `mock-up`, `mock-down`, `mock-test` targets to `src/Makefile` delegating to `mock/Makefile`; leave `FUNCTIONS` and the build untouched
- [X] T042 Ensure the default `pytest` run is unchanged: full suite passes except the two known `tests/contract/test_health.py` failures; `tests/mock_env` is skipped without `MOCK_ENV=1` (one failure, `test_logging`, comes from the uncommitted `raw` log line in `src/lambda_function.py`, not from this feature)
- [ ] T043 Run the quickstart end to end on a machine with Docker and record timings against SC-001 and SC-002 in `specs/008-production-like-mock-env/run-notes.md` **[DOCKER]**  **NOT RUN: needs Docker.**
- [X] T044 Final report: list which tasks were verified in-process only and which on the container stack

## Dependencies and Order

- Phase 1 -> Phase 2 -> user stories. US1 and US2 are both P1; US2 depends on US1's scenario loading and invoke path (T021, T022). US3 depends on Phase 2 and the driver; US4 depends on US1.
- Within Phase 2: T005 -> T006; T007 -> T008; T009; T010 -> T011 -> T012 -> T013; T014 -> T015 once T006 exists.
- Tests before implementation inside each pair (T005/T006, T007/T008, T016/T017, T026/T027).

## Parallel Examples

- Phase 2: T007+T008, T009 and T014 can run alongside T005/T006.
- US1: T016, T018, T019 and T020 touch different files and can run together.
- US2: T028 and T029 in parallel after T027.
- US3: T032 and T033 in parallel.

## Implementation Strategy

- **MVP**: Phases 1, 2 and 3 (US1). Stop and validate on a Docker machine before US2.
- **Incremental**: US2 next (it carries most of the troubleshooting value), then US3, then US4, then Polish.
- **Honesty gate**: in-process unit tests prove the stand-ins' logic; only the **[DOCKER]** tasks prove the stack. Do not mark a **[DOCKER]** task done without running it.

---

## Phase 8: User Story 5 - Real IDP document, mocked everything else (P3, opt-in)

Added after the clarification of 2026-10-24 (FR-017..FR-020, SC-007; design in research R14..R21 and `contracts/real-idp.md`).
Phases 1-7 above are unchanged. Tasks marked **[REAL]** can only be proven with the developer's own access to a real IDP; **[DOCKER]** as before.

**Goal**: with `MOCK_IDP=real`, the function's IDP reads go to the developer's real IDP using their own temporary credentials, and nothing else leaves the machine.
**Independent Test**: unit and in-process tests against a stand-in "real AWS" (`tests/unit/mock/test_realidp*.py`, `test_inprocess_real_idp.py`); on a machine with access, `MOCK_ENV=1 MOCK_IDP=real pytest tests/mock_env/test_real_idp.py`.

- [X] T045 [P] [US5] Write failing tests `tests/unit/mock/test_events.py`: `events.build_event("159112.pdf")` has the shape of `tests/fixtures/sample_event.json` (source `aws.states`, detail-type `Step Functions Execution Status Change`, status `SUCCEEDED`), is recognised by `syteline.eventdoc.is_status_change`, `eventdoc.parse` returns that document id, and an id outside `^[A-Za-z0-9._/-]{1,256}$` is refused with `ValueError`; and `scenarios/generate.py` still writes byte-identical scenario files
- [X] T046 [US5] Implement `mock/edge/events.py` (`build_event(document_id)`, moved from `mock/scenarios/generate.py`, which imports it) to satisfy T045
- [X] T047 [P] [US5] Write failing tests `tests/unit/mock/test_realidp_creds.py` for the credential reader in `mock/edge/realidp.py`: file missing -> status `credentials missing`; expired `AWS_CREDENTIAL_EXPIRATION` -> `credentials expired at <time>`; valid -> `up`; the file is re-read on every call (a refreshed file takes effect without restart); `repr()` and `str()` of the credentials object and every error message contain none of the key, secret or token
- [X] T048 [US5] Implement `Credentials` in `mock/edge/realidp.py` (reads `mock/.idp/creds`, mode 0600 expected; refuses a file readable by group or others with a message) to satisfy T047
- [X] T049 [P] [US5] Write failing tests `tests/unit/mock/test_realidp_routing.py`: IDP-bound classification for the configured stack (`cloudformation` with `StackName`), output bucket (S3 path), tracking table (`TableName`) and `<stack>-LookupFunction-*` (Lambda Invoke); everything else (the jobs table, the credentials secret, the function itself) is not IDP-bound; the read-only allow-list is exactly `cloudformation:DescribeStacks|ListStackResources|DescribeStackResource|DescribeStackResources`, `s3:GetObject`, `dynamodb:GetItem|Query|BatchGetItem`, `lambda:InvokeFunction` on the lookup pattern; `s3:PutObject`, `dynamodb:PutItem` and `dynamodb:DeleteItem` on the IDP table are not
- [X] T050 [US5] Implement `is_idp_bound` and `READ_ONLY` in `mock/edge/realidp.py` to satisfy T049
- [X] T051 [P] [US5] Write failing tests `tests/unit/mock/test_realidp_forward.py` against a fake "real AWS" `werkzeug` server: the forwarded request is signed with the developer's key id (not the function's fake one), carries `X-Amz-Security-Token`, keeps `X-Amz-Target`, `Content-Type`, body and invocation-type headers, uses the S3 signer for S3, goes to the endpoint chosen for the service (`https://<service>[-fips].<region>.amazonaws.com`, overridable for tests), and the function's own signature headers are dropped
- [X] T052 [US5] Implement `RealForward` in `mock/edge/realidp.py` (botocore `SigV4Auth` / `S3SigV4Auth`, endpoint table with FIPS default for `aws-us-gov`, `MOCK_IDP_FIPS=false` to turn off) to satisfy T051
- [X] T053 [US5] Write failing tests `tests/unit/mock/test_edge_real.py`: in real mode an IDP read reaches the fake real server and its answer is returned; the jobs-table and secret calls still go to the mock; a write against the IDP table or bucket is answered `AccessDenied`, never forwarded, and recorded `target: real, allowed: false`; a call the template does not permit is denied before routing; no mock KMS check is made for a real read; expired or missing credentials answer an AWS-worded `ExpiredToken` and nothing falls back to the mock IDP; every call record has `target` (`mock` or `real`); no secret, key id or token appears in any record, response or log line (capture `caplog`); mock mode behaviour is unchanged
- [X] T054 [US5] Wire real mode into `mock/edge/app.py` (optional `real` collaborator; `target` field in `mock/edge/models.py` call records) to satisfy T053
- [X] T055 [P] [US5] Write failing tests in `tests/unit/mock/test_render_env.py` and implement `render_env.render(account=, region=, partition=, ...)`: with the real account, region and identifiers the template's permission resources carry them (`arn:aws-us-gov:lambda:us-gov-east-1:<account>:function:<stack>-LookupFunction-*`, the real bucket, table and key ARNs); defaults are unchanged
- [X] T056 [US5] Write failing tests `tests/unit/mock/test_prepare_real.py` with a stub `aws` on `PATH`, then implement real mode in `mock/edge/prepare.py`: requires `MOCK_IDP_PROFILE`, `MOCK_IDP_STACK`, `MOCK_IDP_BUCKET`, `MOCK_IDP_TABLE`, `MOCK_IDP_DOCUMENT` (each missing one named in the refusal); calls `aws sts get-caller-identity` and `aws configure export-credentials --profile <p> --format env-no-export`; writes `mock/.idp/creds` (0600) and `mock/.idp/mode.json` (no secrets); refuses already-expired credentials and an invalid document id; the function's env file and `rendered.json` never contain the credentials
- [X] T057 [US5] Add `mock/.idp/` to `.gitignore`; add the edge-only volume `./.idp:/run/idp:ro` to `mock/compose.yaml`; add `MOCK_IDP*` pass-through to `prepare` and an `idp-creds` target (re-export without restart) to `mock/Makefile`; write `tests/unit/mock/test_compose_static.py` asserting only the `edge` service mounts `.idp` and no `fn*` or `erp` service does **[DOCKER to run the stack]** (static checks pass; the stack itself not started)
- [X] T058 [US5] Write failing tests then implement real mode in `mock/edge/seed.py` and `mock/edge/control.py`: `GET /scenarios` lists only `real-idp`; `POST /scenario/real-idp` resets and returns `events.build_event(<document>)`; a mock scenario name answers 409 with the reason (modes never mix); `GET /health` has `idp` (`mock`, `up`, or the credential reason) and `ready` is false until it is `up`/`mock`; the status is re-checked every 10 seconds (tests in `tests/unit/mock/test_world_real.py`)
- [X] T059 [US5] Write `tests/unit/mock/test_inprocess_real_idp.py`: a second Moto server stands in for the real IDP (seeded with `IdpSeeder`), the edge in real mode forwards IDP reads to it with re-signing, and the function runs in-process through `lambda_function.lambda_handler`; the outcome is a known one, every IDP read is `target: real`, everything else is `target: mock`, and the mock ERP shows no unexpected write (skipped without the IDP checkout)
- [ ] T060 [US5] Wire real mode into `mock/edge/server.py` (read `mock/.idp/mode.json`, build `RealForward`, skip the mock IDP seeder and its stack) **NOT RUN: needs Docker.**  *(code written and importable; the container path never ran)*
- [ ] T061 [US5] Write `tests/mock_env/test_real_idp.py`, skipped unless `MOCK_ENV=1` and `MOCK_IDP=real`: send `real-idp`; assert only the invariants (outcome known, every `target: real` call is on the allow-list, none denied unless the document is missing, nothing written to the mock ERP unexpectedly); a second test replaces the credentials file with an expired one and asserts `IDP_UNAVAILABLE`, an `ExpiredToken` record and a health reason naming the expiry, then restores it **NOT RUN: needs Docker and real access [REAL].**
- [X] T062 [US5] Extend `docs/MOCK_ENVIRONMENT.md`: a "Real IDP" section (settings, what leaves the machine, credential refresh, what an empty mock database means for the outcome), new fidelity gaps (re-signing only checked against a stand-in; the real key policy applies; the lookup function runs real code), and `real-idp` in the scenarios table; keep `tests/unit/mock/test_parity_static.py` green
- [ ] T063 [US5] Run the quickstart step 8 against the developer's real IDP and record the outcome and timings (SC-007: credential problems reported within 30 seconds) in `specs/008-production-like-mock-env/run-notes.md` **NOT RUN: needs Docker and real access [REAL].**

### Phase 8 dependencies and order

- T045 -> T046; T047 -> T048; T049 -> T050; T051 -> T052 (the four pairs are independent of each other).
- T053/T054 need T048, T050 and T052. T055 is independent. T056 needs T055. T058 needs T046 and T054.
- T059 needs T054, T055 and T058. T060 needs T054, T056 and T058. T061 and T063 need T060.
- T057 needs T056 (the settings it passes through).

### Phase 8 parallel example

- Together: T045, T047, T049, T051 and T055 (five different test files); then T046, T048, T050, T052 (one module `realidp.py` for the last three: do those in order).

### Phase 8 implementation strategy

- Build the testable core first (T045-T059): everything there runs in this repository's normal test run and proves routing, the read-only rule, credential handling and the no-leak rule.
- Only then wire the container entry and run it (T060-T063), which needs Docker and real access.
- Honesty gate as before: a **[DOCKER]** / **[REAL]** task is not marked done until it has been run.
