# Implementation Plan: Production-Like Mock Environment

**Branch**: `008-production-like-mock-env` | **Date**: 2026-10-24 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/008-production-like-mock-env/spec.md`

## Summary

Add a second test layer, `tests/mock_env/`, that runs the **built, unmodified function package** inside the AWS Lambda
Python 3.13 container runtime and points it at network-level stand-ins for everything it calls: a SyteLine SOAP mock,
a **real SQL Server** (TLS, seeded with numbering routines), a mock AWS edge (Moto behind a recording, fault-injecting,
policy-checking proxy), a stub IDP lookup function, a schedule driver and an API-Gateway shim. One command starts it
(`make mock-up`), one resets it, and a pytest suite drives it through a control API. Settings, time limit, memory limit
and permissions are **read from `src/template.yaml`**, so the mock cannot drift silently (FR-008, FR-012).

**Real-IDP mode (added by the clarification of 2026-10-24, FR-017..FR-020, US5):** an opt-in mode in which only the function's IDP reads
leave the mock. The edge re-signs those requests with the developer's own temporary credentials (exported from a profile into a file
only the edge can read) and sends them to the real IDP services; everything else, including the ERP and the database, stays mocked.
See research R14..R21 and `contracts/real-idp.md`.

## Technical Context

**Language/Version**: Python 3.13 in the function container; host tooling runs on the developer's Python (3.9+ for the mock tools and pytest, as the existing suite does)

**Primary Dependencies**: Docker Compose (container runtime), `public.ecr.aws/lambda/python:3.13` (Lambda runtime interface emulator), `mcr.microsoft.com/mssql/server:2022-latest`, Moto server, Werkzeug (HTTP mocks), PyYAML (template), pytest, requests

**Storage**: Moto DynamoDB (jobs table, IDP tracking table), Moto S3 (IDP output bucket), SQL Server container (customer, order and numbering data)

**Testing**: pytest marker `mockenv`, skipped unless `MOCK_ENV=1`; existing unit/contract suites are untouched

**Target Platform**: Developer laptop (macOS arm64/x86, Linux) and CI runner with a container runtime. SQL Server 2022 on Apple silicon needs the emulated x86 image (Docker Desktop with Rosetta)

**Project Type**: Test infrastructure (tools + tests); no change to function behaviour

**Performance Goals**: SC-001 first run < 10 min from fresh checkout; SC-002 reset-to-result < 5 min; SQL Server start-up is the dominant cost (budget 90 s, health-gated)

**Constraints**: No egress (compose `internal` network, SC-005); live ERP writes stay off; credentials never printed; mock data synthetic only (FR-015); function code and `template.yaml` are not edited to suit the mock (FR-004)

**Scale/Scope**: ~6 containers, ~10 scenarios, ~40 integration tests

## Constitution Check

`.specify/memory/constitution.md` is the unfilled template, so there are no ratified gates. Standing project rules from earlier features are applied as gates instead:

| Rule | Status |
|------|--------|
| Live ERP writes OFF by default (`LIVE_WRITES=false`) | Pass: the suite asserts it and the mock ERP rejects nothing it should not receive |
| Credentials/secrets never printed or logged | Pass: test secrets are generated per run, kept out of logs; control API redacts values |
| One function, one entry point, no network resources in the template | Pass: template unchanged |
| Tests first, fakes layer kept | Pass: unit layer unchanged; this is additive |
| Real-IDP mode: credentials never printed, logged, committed, or visible to the function | Pass by design: file mode 0600 under git-ignored `mock/.idp/`, mounted into the edge only, redacted from call records (R16) |
| Real-IDP mode: nothing is written to any real service | Pass by design: read-only allow-list on the real side, everything else refused and recorded (R18) |

Re-check after Phase 1: no violations.

## Project Structure

### Documentation (this feature)

```text
specs/008-production-like-mock-env/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
└── contracts/
    ├── control-api.md      # reset / scenario / faults / calls
    └── mock-services.md    # what each stand-in must answer
```

### Source Code (repository root)

```text
mock/
├── compose.yaml            # services, internal network, health checks
├── Makefile                # up / down / reset / test (included by src/Makefile targets mock-*)
├── render_env.py           # template.yaml -> function env, memory, timeout, IAM statements
├── edge/                   # mock-AWS edge: proxy in front of Moto
│   ├── app.py              #   routing, recording, faults, policy check, Lambda Invoke shim
│   ├── policy.py           #   evaluates IAM statements (Allow/Deny, Resource, Action globs)
│   └── actions.py          #   service+operation -> IAM action table
├── erp/                    # SyteLine SOAP mock + WSDL
│   ├── app.py
│   └── service.wsdl
├── idp/                    # stub LookupFunction + seeded stack outputs/S3/tracking table
├── driver/                 # schedule + API Gateway shim
│   └── app.py
├── sqlserver/
│   ├── init.sql            # types, tables, NextCoSp / NextWOSp stand-ins
│   ├── certs.sh            # per-run CA + server cert (SAN sqlserver)
│   └── mssql.conf
└── scenarios/              # synthetic data: *.json (one folder per scenario)

tests/mock_env/
├── conftest.py             # readiness gate, address allowlist guard, control client, reset fixture
├── test_intake_end_to_end.py
├── test_faults.py
├── test_parity.py          # template vs mock: settings, actions, timeout, permissions
└── test_isolation.py       # repeatability, no-egress, live writes off

docs/MOCK_ENVIRONMENT.md    # usage + fidelity-gap list (FR-016)

# added for real-IDP mode
mock/edge/realidp.py        # routing of IDP requests to the real services, re-signing, read-only allow-list, credential file reader
mock/edge/events.py         # builds the processing-complete event for a document id (shared with scenarios/generate.py)
mock/.idp/                  # git-ignored: creds (0600), mode.json; mounted into the edge container only
tests/unit/mock/test_realidp.py     # against a fake "real AWS" HTTP server
tests/mock_env/test_real_idp.py     # opt-in (MOCK_IDP=real), needs the developer's own access
```

**Structure Decision**: a top-level `mock/` directory for the stand-ins (they are tools, not function code and must not be packaged into the Lambda), `tests/mock_env/` for the suite. The build output is mounted into the runtime image, never copied from a different source.

## Complexity Tracking

| Choice | Why needed | Simpler alternative rejected because |
|--------|-----------|--------------------------------------|
| Own policy evaluator in the edge instead of Moto/LocalStack IAM enforcement | FR-008 needs the *template's* policy applied; enforcement in free LocalStack/Moto is partial and needs real role credentials | Would pass calls production denies (the exact failures we hit) |
| Edge proxy in front of Moto | One place to record, inject faults, check permissions, and route Lambda Invoke | Patching each service separately repeats the logic |
| Real SQL Server | Clarified answer FR-014 | Emulation cannot catch TLS, driver or dialect problems |
