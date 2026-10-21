---

description: "Task list for Stand-alone Public Lambda (No VPC)"
---

# Tasks: Stand-alone Public Lambda (No VPC)

**Input**: Design documents from `/specs/003-standalone-public-lambda/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/standalone-interface.md, quickstart.md

**Tests**: Included (plan.md specifies pytest template, guard and deploy-script tests plus cfn-lint/`sam validate`/`sam build`).

**Organization**: Grouped by user story. Paths are relative to the repo root. This feature adds a second template and stack and a small run-time guard; the VPC deployment (`scr/template.yaml`) and its behavior must not change (SC-005). Feature 001's safety rules still apply (live writes off, web service only, no password in logs).

**Hard rules**: the new template contains no `VpcConfig`, no `AWS::EC2::*` resource, no network parameter, no function URL, no API, no event source and no invoke permission for a public principal (FR-001/002/006). The HTTPS guard is switched on only by the new template (`SYTELINE_REQUIRE_HTTPS=true`), never by default. Nothing is deployed from this environment; tasks that need an AWS account are marked BLOCKED.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1 = deploy with no network preparation (P1), US2 = use the capability (P2), US3 = safe to expose (P3)

---

## Phase 1: Setup

- [X] T001 [P] Add `scr/deploy.standalone.env` to `.gitignore` (next to `scr/deploy.env`)
- [X] T002 [P] Create `scr/deploy.standalone.env.example` with the settings from contracts/standalone-interface.md: `SYTELINE_WSDL` (https:// by default; comment that http:// needs `ALLOW_PLAIN_HTTP=true` and sends credentials unencrypted), `SYTELINE_USER`, `SYTELINE_SECRET_ARN`, `SYTELINE_CONFIG`, `ALLOW_PLAIN_HTTP=false`, `VERIFY_TLS=true`, `LIVE_WRITES=false`, `LIVE_APPROVAL_REF`, `AWS_PROFILE_NAME`; state that no network setting exists for this version

---

## Phase 2: Foundational (blocks all user stories)

- [X] T003 Add a `[standalone.*]` configuration environment to `scr/samconfig.toml` (`stack_name = "sytline-connect-standalone"`, `template_file = "template-standalone.yaml"`, `region = "us-gov-east-1"`, and the same build/validate/deploy parameters as `default`: `CAPABILITY_IAM`, change-set confirmation, managed S3 bucket, lint); the `default` block stays unchanged
- [X] T004 Add a `standalone_template` fixture to `tests/conftest.py` (module-scoped) that loads `scr/template-standalone.yaml` with `load_template(path)` from `tests/cfn_template.py`

**Checkpoint**: configuration environment and test fixture exist.

---

## Phase 3: User Story 1 - Deploy with no network preparation (P1) 🎯 MVP

**Goal**: The stand-alone version deploys with only ERP and credential settings; no function is in a VPC and no network resource exists.

**Independent Test**: `pytest tests/unit/test_template_standalone.py tests/unit/test_deploy_script.py -k standalone`; `./deploy.sh --standalone --validate` and `--build`; real deployment is BLOCKED (T024).

### Tests

- [X] T005 [P] [US1] Structure tests `tests/unit/test_template_standalone.py` (using `standalone_template`): the resources are exactly `DepsLayer` and `DirectFunction` (logical IDs shared with the VPC template so the existing `Makefile` builds both); `DirectFunction` has no `VpcConfig`; **no resource of any `AWS::EC2::*` type**; no `Events`, `FunctionUrlConfig` or `AWS::Lambda::Url`; no parameter whose name starts with `Worker`, `Host`, `Plant` or equals `ApiVpcEndpointId`, `CreateApiEndpoint`, `CreateVpcEndpoints`, `DynamoDbPrefixListId`; the layer name differs from the VPC template's (`sytline-connect-deps`) so both stacks coexist; there are no `Export`s or cross-stack imports; the handler is `lambda_function.lambda_handler` and the runtime/architecture match the VPC template; the function does not reference the job table or any other stack's resource
- [X] T006 [P] [US1] `deploy.sh` standalone-mode tests in `tests/unit/test_deploy_script.py` (stub `sam`/`aws`, using the existing helpers): `--standalone` runs `sam build`, `sam validate` and `sam deploy` with `--config-env standalone`, reads `deploy.standalone.env` (not `deploy.env`), passes the ERP/credential parameters, does not call `aws cloudformation describe-stacks` for the create/supply guard, works with `--build`/`--validate` without a settings file, and honors `--profile`; a network setting present in the file (each of `WORKER_VPC_ID`, `WORKER_SUBNET_IDS`, `WORKER_SG_IDS`, `WORKER_ROUTE_TABLE_IDS`, `HOST_NETWORK_CIDR`, `PLANT_CIDR_1`, `DYNAMODB_PREFIX_LIST_ID`, `API_VPCE_ID`, `CREATE_API_ENDPOINT`, `CREATE_VPC_ENDPOINTS`) is refused as "not applicable to the stand-alone version" naming it, before any `sam` call; missing required settings are refused

### Implementation

- [X] T007 [US1] Create `scr/template-standalone.yaml` per data-model.md: parameters `SytelineWsdlHttps` (pattern `^$|^https://[^ ]+$`), `SytelineWsdlHttp` (pattern `^$|^http://[^ ]+$`), `AllowPlainHttp`, `SytelineUser`, `SytelineSecretArn`, `SytelineConfig`, `LiveWrites`, `VerifyTls`, `UseFipsEndpoints`; the two rules `ExactlyOneWsdl` and `PlainHttpNeedsOverride` with the exact messages in contracts/standalone-interface.md; condition `UsePlainHttp`; template `Metadata` `cfn-lint` ignoring `W1030` (empty defaults); `DepsLayer` (`LayerName: sytline-connect-standalone-deps`, `BuildMethod: makefile`) and `DirectFunction` (`BuildMethod: makefile`, python3.13, x86_64, `CodeUri: .`, timeout 60, memory 256, the layer, no `VpcConfig`, environment `SYTELINE_WSDL` via `!If [UsePlainHttp, ...]`, `SYTELINE_USER`, `SYTELINE_CONFIG`, `SYTELINE_SECRET_ARN`, `SYTELINE_LIVE_WRITES`, `SYTELINE_REQUIRE_HTTPS: "true"`, `SYTELINE_ALLOW_HTTP`, `SYTELINE_VERIFY_TLS`, `AWS_USE_FIPS_ENDPOINT`, and only a `secretsmanager:GetSecretValue` policy on the one secret); outputs `DirectFunctionName`, `DirectFunctionArn`, `PlainHttpOverride`, `LiveWrites`
- [X] T008 [US1] Add `--standalone` mode to `scr/deploy.sh`: select `--config-env standalone` for every `sam` command; read `scr/deploy.standalone.env` instead of `deploy.env` (still optional for `--build`/`--validate`); require `SYTELINE_WSDL`, `SYTELINE_USER`, `SYTELINE_SECRET_ARN`; refuse the network settings listed in contracts/standalone-interface.md as not applicable (naming them); skip the create/supply mode-switch guard; keep `--profile` and the live-writes approval check unchanged; pass `SytelineUser`, `SytelineSecretArn`, `SytelineConfig`, `LiveWrites` as quoted overrides (the address, override and TLS parameters are completed in T019)

**Checkpoint**: US1 in place; both templates lint, validate and build.

---

## Phase 4: User Story 2 - Use the capability without the private network (P2)

**Goal**: The connectivity check (with the dry run) and the notes operations give the same results and error codes as in the VPC version; order upload is absent.

**Independent Test**: `pytest tests/unit/test_standalone_parity.py`.

- [X] T009 [P] [US2] Parity tests `tests/unit/test_standalone_parity.py`: run the direct handler under the stand-alone environment (`SYTELINE_REQUIRE_HTTPS=true`, an `https://` address, no `JOBS_TABLE`, no worker name) and under the VPC environment: the dry-run health stages (`load, dns, tcp, wsdl`) and result keys are equal; `add_note`/`get_notes` (with `FakeSoap` through the client factory) return identical shapes, states and error codes (`NOT_FOUND`, `VALIDATION`, `LIVE_WRITES_DISABLED` by default); `create_order` and any other action return `UNKNOWN_ACTION`; importing `lambda_function` pulls in no job-store, order-API, worker or sweeper module
- [X] T010 [US2] Confirm `scr/lambda_function.py` has no dependency on the job store, the order handlers or VPC-only settings under the stand-alone environment (satisfy T009; no code change is expected, fix any hidden dependency found)

**Checkpoint**: US1 + US2 work.

---

## Phase 5: User Story 3 - Safe to expose to the internet (P3)

**Goal**: Only permitted cloud credentials can call it, there is no public address, the ERP connection is HTTPS unless the deployer explicitly overrides, and nothing secret leaks.

**Independent Test**: `pytest tests/unit/test_template_standalone.py tests/unit/test_transport.py tests/unit/test_deploy_script.py`.

### Tests

- [X] T011 [P] [US3] Safety and rules tests in `tests/unit/test_template_standalone.py` (via `tests/cfn_eval.py`): no public exposure (no resource policy or `AWS::Lambda::Permission` with principal `*`, no URL, no API); the execution policy allows only `secretsmanager:GetSecretValue` on the single secret parameter (no wildcard action or resource); defaults are `LiveWrites=false`, `VerifyTls=true`, `AllowPlainHttp=false`; the function environment has `SYTELINE_REQUIRE_HTTPS` = `"true"` and no password variable; the rules: https address only → accepted; http address with `AllowPlainHttp=true` → accepted; http address without the override → refused with the `PlainHttpNeedsOverride` message; no address or both addresses → refused with `ExactlyOneWsdl`; an `http://` value in the https parameter or the reverse → parameter pattern error; `SYTELINE_WSDL` resolves to the right address in each accepted case; outputs include `PlainHttpOverride`
- [X] T012 [P] [US3] Transport guard tests `tests/unit/test_transport.py`: with `SYTELINE_REQUIRE_HTTPS=true` and an `http://` address and no override, `health` (normal and dry run) and `add_note`/`get_notes` return `ok:false` with code `INSECURE_TRANSPORT` and a message saying what to change, **before any network call or client creation** (assert no socket, no SOAP client, no credentials read); with the override on they proceed and `health` adds the `warnings` entry about unencrypted credentials without changing its stage list; with an `https://` address nothing changes; with the guard off (the VPC version) an `http://` address works exactly as before and no `warnings` entry appears; `SoapIdoClient` refuses an insecure address even if called directly; an address that changes from `https://` to `http://` after deployment is refused on the next call
- [X] T013 [P] [US3] `deploy.sh` HTTPS/override/approval tests in `tests/unit/test_deploy_script.py`: an `https://` `SYTELINE_WSDL` is passed as `SytelineWsdlHttps` and `SytelineWsdlHttp` is not passed; an `http://` one without `ALLOW_PLAIN_HTTP=true` is refused before any `sam` call, naming the override; with `ALLOW_PLAIN_HTTP=true` it is passed as `SytelineWsdlHttp` with `AllowPlainHttp="true"` and a warning that credentials cross the network unencrypted is printed; `VERIFY_TLS` defaults to `true` and is passed as `VerifyTls`; `LIVE_WRITES=true` without `LIVE_APPROVAL_REF` is refused and with it passes `LiveWrites="true"`; a scheme other than http/https is refused

### Implementation

- [X] T014 [P] [US3] Add to `scr/syteline/errors.py` the code `INSECURE_TRANSPORT` and the exception `InsecureTransport`; add to `scr/syteline/config.py` `require_https()` (true only when `SYTELINE_REQUIRE_HTTPS` is exactly on), `allow_http()` (`SYTELINE_ALLOW_HTTP`) and `transport_problem(url)` returning a message (naming the override and the `https://` fix) when the guard is on, the scheme is `http` and the override is off, else `None`
- [X] T015 [US3] Apply the guard in `scr/syteline/health.py` `run_health`: before any DNS/TCP/WSDL/token work, and in dry run too, return `{"ok": false, "dry_run": ..., "error": ..., "errors": [{"code": "INSECURE_TRANSPORT", ...}], "stages": []}`; when the override is on and the address is `http://`, add `"warnings": ["plain HTTP override is on: credentials cross the network unencrypted"]` to the result without changing the stages
- [X] T016 [US3] Apply the guard in `scr/lambda_function.py` before the notes actions build a client (return the `INSECURE_TRANSPORT` failure) and in `scr/syteline/client.py` `SoapIdoClient` before the SOAP client is created (raise `InsecureTransport`; the notes mapping turns it into the same error code)
- [X] T017 [US3] Complete `--standalone` in `scr/deploy.sh`: choose `SytelineWsdlHttps` or `SytelineWsdlHttp` by the scheme of `SYTELINE_WSDL` (refuse other schemes); refuse `http://` unless `ALLOW_PLAIN_HTTP=true` and print the unencrypted-credentials warning when it is; pass `AllowPlainHttp`, `VerifyTls` (default `true`) and `LiveWrites` (with the approval check); mark `PlainHttpOverride` in the printed summary
- [X] T018 [P] [US3] Create `docs/STANDALONE.md` (FR-010, SC-006): when this version suits (ERP reachable from the internet: test/staging, vendor-hosted, or published through a secure gateway) and when it does not (ERP only on the plant network → use the VPC version); a comparison table with the VPC version (network, who can call, outbound address, features included); the fixed-outbound-address caveat (no ERP allow-listing); HTTPS by default and the plain-HTTP override and its consequence; TLS verification; how to grant a principal `lambda:InvokeFunction` and how to invoke; deploy (`./deploy.sh --standalone`), verify (dry-run health check; a caller without permission is denied), coexistence with the VPC stack, and teardown (`sam delete --stack-name sytline-connect-standalone --config-env standalone`)
- [X] T019 [P] [US3] Add pointers to the stand-alone version in `README.md` and `docs/DEPLOYMENT.md`, and document the new `INSECURE_TRANSPORT` code and the optional `warnings` entry in `specs/001-syteline-erp-integration/contracts/lambda-actions.md` as stand-alone-only additions

**Checkpoint**: all three stories done.

---

## Phase 6: Polish & Cross-Cutting

- [X] T020 Full local validation: `cfn-lint scr/template-standalone.yaml` and `scr/template.yaml`; `sam validate --lint --region us-gov-east-1` for both (`-t scr/template-standalone.yaml --config-env standalone` for the new one); `./deploy.sh --standalone --build` and `./deploy.sh --build` (the built stand-alone template keeps both rules and has no `VpcConfig`); the whole pytest suite; confirm `scr/template.yaml` is unchanged by this feature (`git diff` shows nothing for it) so the VPC deployment is untouched (SC-005)
- [X] T022 [US4] (revision 2026-10-21) Optional attachment to an existing VPC, subnets and security group in `scr/template-standalone.yaml`: parameters `AttachVpcId`/`AttachSubnetIds`/`AttachSecurityGroupIds` with identifier patterns, condition `AttachToVpc`, rule `AttachmentAllOrNothing`, conditional `VpcConfig`, outputs `NetworkMode` and `AttachedVpcId`; an explicit least-privilege `DirectFunctionRole` (network-interface permission only when attached) replaces SAM `Policies`; tests in `tests/unit/test_template_standalone.py`
- [X] T023 [US4] `deploy.sh --standalone`: accept `ATTACH_VPC_ID`, `ATTACH_SUBNET_IDS`, `ATTACH_SG_IDS` (all three or none, formats checked), pass them as `AttachVpcId`/`AttachSubnetIds`/`AttachSecurityGroupIds`, print the network reminder; every other network setting stays "not applicable"; tests in `tests/unit/test_deploy_script.py`; `scr/deploy.standalone.env.example` updated
- [X] T024 [US4] FR-013: `scr/syteline/config.py` reads the secret with short timeouts and raises `SecretStoreUnavailable` on any failure; `health` and the notes actions return `SECRET_STORE_UNREACHABLE` promptly; `diagnose` still runs (from the non-secret configuration); the diagnostics finding about the credentials store now explains the endpoint/NAT fix; tests in `tests/unit/test_secret_store.py` and `tests/unit/test_diagnostics.py`
- [X] T025 [US4] Docs: `docs/STANDALONE.md` (two modes, section 3a, troubleshooting), README, `specs/001-syteline-erp-integration/contracts/lambda-actions.md` (`SECRET_STORE_UNREACHABLE`), and this feature's plan, research, data-model, contracts and quickstart
- [ ] T021 BLOCKED (needs an AWS GovCloud account and an ERP reachable from the internet): run `specs/003-standalone-public-lambda/quickstart.md` steps 6–8 (deploy, console shows "no VPC", dry-run health check with permitted credentials, denial for credentials without invoke permission, an https ERP and an `http://` ERP with and without the override, coexistence with the VPC stack, teardown) and record the results there

---

## Analysis findings applied during implementation

The pre-implementation analysis found gaps that were fixed in the build (the task texts above describe the original scope):
- **S1 (HIGH)**: the HTTPS guard also checks the SOAP service address **declared in the WSDL** (zeep sends calls, including the password, there even when the WSDL itself was fetched over https://); refused before the token stage, fail-closed if unreadable (`syteline/client.py`, `syteline/health.py`; tests in `tests/unit/test_transport.py`).
- **T1**: the guard runs after configuration is resolved (the secret can supply the address) and before any ERP-bound traffic or client creation; a test covers a secret-supplied `http://` address.
- **L1**: the template address patterns reject `@` (no credentials in the address), `deploy.sh --standalone` refuses `user:pass@host`, and guard messages never repeat the address.
- **B1**: `samconfig.toml` gives the stand-alone configuration its own `build_dir` (`.aws-sam/standalone`) and deploy `template_file`.
- **U1/U2/A1/G1**: `docs/STANDALONE.md` notes cross-account callers need a resource policy; `AWS_PROFILE_NAME` is read from `deploy.standalone.env` (tested); the structural test proves only the absence of public exposure (denial is verified live in T021); the guard-off test (`tests/unit/test_transport.py`) and the untouched `scr/template.yaml` are the SC-005 checks.

## Dependencies & Execution Order

- Phase 1 → Phase 2 → stories. T004 needs T007 only at run time (tests are written first and fail until then); T003 before T008.
- US1: tests T005, T006 before T007, T008; T007 before T008's template-dependent assertions pass; T008 is `deploy.sh` (also edited by T017: sequential).
- US2: T009 can be written once T007 exists (it uses the direct handler, which already exists); T010 is a check of existing code.
- US3: tests T011–T013 before T014–T017; T014 → T015 → T016 (T014 first; T015 and T016 use it); T017 needs T008 and T014; T018 and T019 can start after T007 and T017.
- Polish: T020 last among code tasks; T021 last and BLOCKED.

### Parallel opportunities

- Setup: T001, T002. US1 tests: T005, T006. US2: T009. US3 tests: T011, T012, T013 together; T014 alongside; T018, T019 together.

## Implementation Strategy

1. **MVP**: Phases 1–3 (T001–T008): the stand-alone stack deploys with no network input and is proven by template tests and the linter. Because the HTTPS guard and rules are the heart of the safety story, do not release or deploy the MVP beyond a test account before US3 (T011–T017) is done.
2. Add US2 (parity), then US3 (guard, rules tests, docs), then Polish.
3. T021 needs a real account and a reachable ERP; nothing before it does.
4. After each template or script task run `cfn-lint` and the relevant pytest file; T020 repeats the whole set.
