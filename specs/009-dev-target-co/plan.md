# Implementation Plan: Development Mode Against a Pre-Created Customer Order

**Branch**: `009-dev-target-co` | **Date**: 2026-10-26 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/009-dev-target-co/spec.md`

## Summary

A deployment setting `DEV_TARGET_CO` (template parameter `DevTargetCo`, replacing the test setting `CO_NUMBER`) turns on **development mode**. In it: reservation uses the target as the CO number (no `NextCoSp`); the save **updates** the existing CO header and lines instead of inserting (adding lines that are missing), and updates the job when `JOB_NUMBER` is also set and the job exists; before any write it confirms the target exists and belongs to the customer the document resolves to; and the event intake no longer lets the per-document claim or an earlier finished reservation short-circuit a run. Tracker rows are still written, marked `dev: true` with the target. See [research.md](research.md) for decisions, [data-model.md](data-model.md), [contracts/dev-mode.md](contracts/dev-mode.md), [quickstart.md](quickstart.md).

## Technical Context

**Language/Version**: Python 3.13 (Lambda), SAM template

**Primary Dependencies**: existing: zeep SOAP client, boto3, moto (tests); none added

**Storage**: existing DynamoDB jobs table (reservation, document marker, event-claim items); one extra attribute `dev_target` on them; no new table

**Testing**: pytest unit/contract suites with the fake SOAP (`tests/fakes.py`) and the mock ERP (`mock/erp/app.py`, update support added); mock-env scenarios

**Target Platform**: AWS Lambda (GovCloud), single function, existing VPC

**Project Type**: single Lambda project

**Performance Goals**: no change; development mode adds 2-3 ERP reads before the first write

**Constraints**: live writes stay off by default; update key shape and the ERP's acceptance of updates to CO rows are unverified against the real ERP; nothing is deleted

**Scale/Scope**: ~8 source files, ~15 tests

## Constitution Check

`.specify/memory/constitution.md` is the unfilled template; standing project rules are the gates.

| Rule | Status |
|------|--------|
| Live ERP writes OFF by default | Pass: dev mode does not enable them (FR-012) |
| Never delete, never retry an uncertain write | Pass: overwrite is update/insert only; partial overwrite parks as needs_reconciliation |
| Credentials never logged | Pass: no change |
| One function, no network resources | Pass: only a parameter and an env var added |
| Tests first, fakes layer kept | Pass |

Re-check after Phase 1: no violations.

## Project Structure

### Documentation (this feature)

```text
specs/009-dev-target-co/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/dev-mode.md
└── tasks.md             # /speckit-tasks
```

### Source Code

```text
src/
├── template.yaml            # DevTargetCo param -> DEV_TARGET_CO; CoNumber param/CO_NUMBER removed
├── deploy.sh, deploy.env.example   # DEV_TARGET_CO validation + warning; CO_NUMBER removed
├── handlers/event_intake.py # dev mode skips already-processed / claim short-circuits
└── syteline/
    ├── config.py            # dev_target_co() replaces fixed_co_number()
    ├── devmode.py           # NEW: target check (exists, customer matches), dev markers
    ├── reserve.py           # target as CO number, source "development", record dev_target
    ├── savewf.py            # header/line/job: update-or-insert when dev mode
    ├── eventstore.py        # claim_dev: unconditional claim record for a dev run
    └── reservations.py      # dev_target attribute on create
mock/erp/app.py              # Update on existing CO/line/job rows
tests/unit/                  # test_devmode.py (new), updates to test_savewf/test_reserve/test_event_intake/test_config
docs/                        # EVENT_INTAKE.md, SAVE_WORKFLOW.md, DEPLOYMENT.md
```

**Structure Decision**: single project; one new small module (`devmode.py`) so the existing insert path stays untouched when the setting is empty (SC-004).

## Complexity Tracking

No violations.
