# Implementation Plan: One Function, Always in an Existing VPC

**Branch**: `007-single-function-existing-vpc` | **Date**: 2026-10-23 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/007-single-function-existing-vpc/spec.md`

## Summary

Collapse the eight Lambda functions of the plant stack and the stand-alone stack into **one function**, `lambda_function.lambda_handler`, that routes by event kind and by `action` (research.md R1 to R3): API Gateway requests by their route, the scheduled cleanup and the self-started background work by explicit actions, the IDP event by default. Remove everything that creates a network (VPC, subnets, route table, security groups, egress rules, endpoints) and the settings that fed it; the function is always attached to a VPC, subnets and security groups the deployer supplies (R4). Keep the private API in front of the one function with an existing `execute-api` endpoint (R5). Replace the old worker's "reserved concurrency of one" by a lease lock in the jobs table so ERP writes stay serialized without throttling everything else (R6), give each action its own time budget so API routes still stop within the API's limit (R7), and retire the stand-alone stack, its https-only guard and its tests (R9). Protect the jobs table and document the move from the old shape (R10, R11).

## Technical Context

**Language/Version**: Python 3.13 (Lambda; existing code), SAM/CloudFormation YAML

**Primary Dependencies**: existing only (zeep, python-tds, rapidfuzz, boto3, idp_sdk via `IDP_REPO`)

**Storage**: the existing jobs table (+ one lock item kind `LOCK#erp-writes`); unchanged otherwise

**Testing**: pytest with the existing fakes; template tests rewritten for the single-function shape; no real AWS call

**Target Platform**: AWS GovCloud us-gov-east-1, one Lambda in an existing VPC

**Project Type**: serverless service, consolidation of an existing project

**Performance Goals**: a health check or status request answered within 10 s while a background save runs (SC-004); reserve of 25 lines within the API's time limit

**Constraints**: no network resource created (SC-001); exactly one `AWS::Serverless::Function` (SC-001); the function name is fixed (`<stack>-fn`) so it can start itself without a circular reference (R8); the old API paths, bodies and IAM authorisation unchanged (FR-005); the jobs table and its data untouched by the move (FR-009)

**Scale/Scope**: tens of documents and uploads a day; one save at a time

## Constitution Check

`.specify/memory/constitution.md` is the unfilled template: no ratified gates. The spec's rules and the standing rules of features 001 to 006 were applied; one is relaxed on purpose and stated: the https-only ERP rule of the stand-alone stack is retired with that stack (spec Assumptions), and the role becomes the union of eight roles (FR-011).

## Project Structure

### Documentation (this feature)

```text
specs/007-single-function-existing-vpc/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── dispatch.md            # which event or action runs what; payloads of the self-started work
│   └── deployment.md          # template parameters, rules, resources, deploy.env settings, migration
└── tasks.md                   # created by /speckit-tasks
```

### Source Code (repository root)

```text
src/
├── lambda_function.py         # the one entry point: event-kind and action routing, per-action time budget, lock for background work
├── handlers/                  # unchanged logic; intake/status/reserve/save_intake/reservation_status keep their API-event handlers,
│   ├── worker.py              #   worker: `work_upload(payload)` / `work_save(payload)` entry points under the ERP-write lock
│   ├── sweeper.py             #   sweeper: `sweep(payload)`; re-drive payloads carry `action`
│   └── routing.py             # NEW: API route table and the budget wrapper (get_remaining_time_in_millis capped per action)
├── syteline/
│   ├── erplock.py             # NEW: lease lock in the jobs table (acquire/wait/release), serializes ERP writes
│   ├── savestart.py, handlers/intake.py, handlers/sweeper.py   # start the background work with an `action` payload, on this same function
│   ├── config.py              # worker_function_name() falls back to AWS_LAMBDA_FUNCTION_NAME; remove require_https/allow_http/
│   │                          #   transport_problem/attached/db_check_mode and their env settings
│   ├── health.py, client.py   # remove the stand-alone branches (https guard, DatabaseCheck, SYTELINE_ATTACHED)
│   └── errors.py              # remove ACTION_UNAVAILABLE and INSECURE_TRANSPORT usage
├── template.yaml              # rewritten: 1 function, API with 5 routes, schedule, optional event rule, table (Retain), layer; no network
├── template-standalone.yaml   # DELETED
├── samconfig.toml             # standalone configuration removed
├── Makefile                   # one function target
├── deploy.sh, deploy.env.example   # one deployment: 3 network settings + API endpoint; no standalone branch or block
docs/                          # STANDALONE.md deleted; DEPLOYMENT, README, TESTING, DATABASE, EVENT_INTAKE, new MIGRATION.md
tests/
├── unit/ test_dispatch.py (NEW), test_erplock.py (NEW), test_budget.py (NEW), test_template_single.py (NEW, replaces
│         test_template_network/rules/standalone/reservation/event shape checks), test_deploy_script.py (rewritten for one deployment)
└── DELETED: test_mode_parity.py, test_standalone_parity.py, test_template_standalone.py, the https-guard cases of test_transport.py
```

**Structure Decision**: the handlers keep their code and tests (they take an API event or a payload); only the entry point, the template, the deploy script and the guard/mode code change. The router is the one new piece of logic; the lock is the one new safeguard.

## Complexity Tracking

| Item | Why needed | Simpler alternative rejected because |
|---|---|---|
| A lease lock in the jobs table | FR-010: with one function the old worker's reserved concurrency of one would also throttle every API call | Reserved concurrency 1 on the single function breaks FR-008 |
| Per-action time budgets | An API request must stop at the API's limit although the function's timeout is 300 s for the background save | Using the function timeout for API actions lets work continue after the caller timed out |
| A fixed function name | The function must be allowed to start itself; a role statement naming the function by `!Ref` is circular | A wildcard on all functions widens the role beyond this function |
