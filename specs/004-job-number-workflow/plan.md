# Implementation Plan: Job Number Reservation and Save Workflow

**Branch**: `004-job-number-workflow` | **Date**: 2026-10-21 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/004-job-number-workflow/spec.md`

## Summary

Add a two-stage workflow to the VPC deployment's private API: **reserve** (synchronous: resolve the customer, then obtain one CO number and one job number per PO line from SyteLine's own numbering routines through the web service, return them, write no order/line/job) and **save** (asynchronous, same pattern as the existing upload: accept, return immediately, a background run saves the CO header, then per line the CO line carrying its job number, the teardown date, and the job, then reads everything back). Reservations live in the existing jobs table, are single-use, bound to the PO fingerprint and the caller, and keep consumed numbers accounted for. The save run reuses the existing upload code (`Upload._insert`, `_verify`-style read-back, safety states, never replay) and the single serialized worker. The existing one-step upload, notes and health actions are unchanged. The workflow is part of the VPC deployment only. See [research.md](research.md).

**Revision 2026-10-21 (document source):** reserve and save take an IDP **document ID**; the customer, PO number and lines are read from the IDP result through the IDP SDK (research.md R12 to R17) and are never stored in our table. New thin wrapper `src/syteline/idp.py` (`IdpSource`) and mapping `src/syteline/idp_fields.py`; reserve, save intake and the worker call it; the reservation item keeps only `document_id`, fingerprint and results.

## Technical Context

**Language/Version**: Python 3.13 (Lambda; existing code), SAM/CloudFormation YAML

**Primary Dependencies**: zeep, lxml, requests, boto3 (existing); no new dependency

**Storage**: the existing DynamoDB jobs table, new item types `RESERVATION#<id>` and `RESREF#<request_ref>` (no new table)

**Testing**: pytest (existing suite, fakes in `tests/fakes.py`): unit tests of reservation and save logic, contract tests of the three routes, worker/sweeper tests, template structure tests; `sam validate --lint`; no real ERP or deployment from this environment

**Target Platform**: AWS GovCloud us-gov-east-1; Lambda in the plant-routed VPC; private API Gateway

**Project Type**: serverless service, extension of an existing project

**Performance Goals**: reserve returns for a 20-line PO in under 30 s (SC-001); save is asynchronous and bounded by the worker's 300 s

**Constraints**: ERP only through the web service, no SQL (FR-015); live writes gate applies to reserving (FR-014); no automatic delete/retry/replay (FR-010); reserve must fit API Gateway's 29 s limit, so it is capped at 25 lines and stops safely if time runs out; ERP writes stay serialized through the single worker; the existing actions' behavior unchanged (FR-017)

**Scale/Scope**: tens of POs per day, up to 25 lines each

## Constitution Check

`.specify/memory/constitution.md` is still the unfilled template, so there are no ratified gates. The spec's own rules (FR-001 to FR-017) and the project's standing rules from features 001 to 003 (web service only, confirmed-only requests, never replay an uncertain write, no secrets in logs or responses) were applied. **Result: PASS (no violations).** Post-design re-check: PASS.

## Project Structure

### Documentation (this feature)

```text
specs/004-job-number-workflow/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── api.md              # three new routes on the private API
└── tasks.md                # created by /speckit-tasks
```

### Source Code (repository root)

```text
src/
├── template.yaml                  # + ReserveFunction (VPC, ERP + table access), SaveIntakeFunction, ReservationStatusFunction, three routes on OrderApi; worker/sweeper env and policy unchanged except route to save
├── handlers/
│   ├── reserve.py                 # NEW: POST /job-reservations
│   ├── save_intake.py             # NEW: POST /job-reservations/{id}/save
│   ├── reservation_status.py      # NEW: GET /job-reservations/{id}
│   ├── worker.py                  # + dispatch: an event with reservation_id runs the save run
│   └── sweeper.py                 # + re-drive save_accepted, park stale saving reservations
└── syteline/
    ├── reservations.py            # NEW: reservation store + state machine (like jobs.py)
    ├── reserve.py                 # NEW: resolve customer, live-writes check, allocate CO + N job numbers, record each as obtained
    ├── savewf.py                  # NEW: SaveRun(Upload): header, per line line+teardown+job, read-back, per-line results
    ├── idp.py                     # NEW (revision): IdpSource: fetch status, result and confidence through the IDP SDK; gate; map to PO details
    ├── idp_fields.py              # NEW (revision): document class and field paths (UNCONFIRMED until supplied)
    ├── validation.py              # + validate_reserve(), validate_save(), fingerprint()
    ├── errors.py                  # + codes (RESERVATION_USED, RESERVATION_MISMATCH, RESERVATION_FORBIDDEN, RESERVATION_NOT_READY, TOO_MANY_LINES)
    ├── idos.py                    # + header/line/teardown property names marked UNCONFIRMED
    └── client.py                  # + update(ido, key, values) (gated by live writes, ErpUncertain on transport failure)
docs/
└── RESERVATIONS.md                # NEW: the two-stage flow, states, accounting for gaps
tests/
├── unit/test_reserve.py, test_savewf.py, test_reservations_store.py, test_template_reservation.py  # NEW
└── contract/test_reservation_routes.py                                                            # NEW
```

**Revision, source layout:** the three handlers and `savewf.py` stop reading PO details from the request or the table: `reserve.py` and `save_intake.py` call `IdpSource.po_details(document_id)`; the worker calls it again before writing. `template.yaml` gains `IDP_STACK_NAME` / `IDP_REGION` environment variables and the read permissions for the IDP stack on the reserve, save-intake and worker functions, and the VPC needs a private path to S3 and CloudFormation (or NAT) for them.

**Structure Decision**: extend the existing `src/` package with the same layering as the upload (handlers thin, `syteline/` logic, a store module for state, one worker for ERP writes). `SaveRun` subclasses `Upload` so the proven write bookkeeping (record "possibly written" before sending, classify rejected/uncertain/unreachable, time budget) is reused instead of copied.

## Complexity Tracking

| Item | Why needed | Simpler alternative rejected because |
|---|---|---|
| A separate synchronous Lambda for reserve, in the VPC | Reserving needs the ERP and the secret, and the caller wants the numbers in the response; the existing intake function is outside the VPC | Making reserve asynchronous (accept, then poll) works but adds a round trip for a step that takes seconds, and the spec wants the numbers returned |
| Reuse of the single serialized worker for save | Keeps all ERP writes serialized (existing rule) | A second worker would allow concurrent writes to the same ERP |
| A new client `update` operation | The demo sets the teardown date as a separate step after the line exists, and the spec makes its failure a warning (FR-007) | Writing teardown inside the line insert (as feature 001 does) cannot fail separately, so FR-007 could not be met |
| `IdpSource` wrapper over the SDK, fetched three times (reserve, save intake, worker) | the PO data must not be stored (FR-021) yet must be current at write time and checked against what was reserved (FR-020) | storing a copy at reserve is exactly the duplication being removed |
| SDK packaged in the Lambda, with a direct-read fallback | the SDK is the requested interface; its weight (idp_common) may exceed layer limits | direct reads only would bypass the SDK the owner asked for |
