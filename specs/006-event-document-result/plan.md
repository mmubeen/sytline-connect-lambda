# Implementation Plan: Document Result from a Processing-Complete Event

**Branch**: `006-event-document-result` | **Date**: 2026-10-22 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/006-event-document-result/spec.md`

## Summary

Add a second way to start the job-number workflow (feature 004): a new function in the plant deployment that the document-processing service's "Step Functions Execution Status Change" event starts. It (1) parses the event and takes the document ID from the execution `input`/`output` text, ignoring or refusing events that cannot be used, (2) reads the document's current result once through the IDP SDK (never the event's snapshot), builds the scan result summary, (3) when the result is usable and live writes are on, reserves the CO and job numbers with the existing `run_reserve` and accepts the save and starts the existing worker, exactly once per document, and (4) records the outcome so a repeat delivery returns it. No new ERP behaviour: all safeguards are those of features 004 and 005. The IDP SDK usage is the one already in `src/syteline/idp.py`; the SDK documentation was run through the document reviewer (research.md R1).

## Technical Context

**Language/Version**: Python 3.13 (Lambda; existing code), SAM/CloudFormation YAML

**Primary Dependencies**: existing only (idp_sdk via `IDP_REPO`, boto3, zeep, python-tds, rapidfuzz); the EventBridge rule is a template resource, no library

**Storage**: the existing jobs table (new item kinds `DOCRES#<document_id>` and `EVTDOC#<document_id>`; no PO details stored, feature 004 FR-021)

**Testing**: pytest with the existing fakes (`FakeIdp`, `FakeSoap`, `FakeJobsTable`, `FakeLambda`, `FakeSqlServer`), a repaired copy of the example event as a fixture, template tests

**Target Platform**: AWS GovCloud us-gov-east-1; plant VPC deployment only (the stand-alone deployment has no reserve or save)

**Project Type**: serverless service, extension of an existing project

**Performance Goals**: summary under 10 s (SC-001); a 25-line event run to "save accepted" under 60 s (SC-007; the save itself is the existing asynchronous worker)

**Constraints**: read-only until a result is usable and live writes are on; once per document; no automatic retry of the function (a replayed reservation burns numbers); logs without field values (FR-009); the event and the IDP service must be in the same account and region as the plant stack (EventBridge rule limitation)

**Scale/Scope**: tens of documents per day, up to 25 lines each

## Constitution Check

`.specify/memory/constitution.md` is still the unfilled template: no ratified gates. The spec's own rules and the standing rules of features 001 to 005 were applied; none is relaxed. Reserving and writing stay behind the live-writes switch.

## Project Structure

### Documentation (this feature)

```text
specs/006-event-document-result/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── event-intake.md       # event shape, response/outcome, template parameter, error codes
└── tasks.md                  # created by /speckit-tasks
```

### Source Code (repository root)

```text
src/
├── template.yaml               # + EventIntakeFunction, IdpStateMachineArn parameter, rule (only when the ARN is given), no retries
├── Makefile                    # + EventIntakeFunction build target
├── deploy.sh                   # + IDP_STATE_MACHINE_ARN (plant branch only, always passed; format check)
├── deploy.env.example          # + IDP_STATE_MACHINE_ARN in the PLANT ONLY block
├── handlers/
│   └── event_intake.py         # NEW: parse -> read once -> summary -> reserve -> accept save -> outcome
└── syteline/
    ├── eventdoc.py             # NEW: event parsing (document ID, status, execution, event date), refusals
    ├── idp.py                  # + read_document(), scan_summary(), po_details(document_id, read=None)
    ├── eventstore.py           # NEW: the once-per-document guard and outcome record (EVTDOC#), DOCRES# marker lookup
    ├── savestart.py            # NEW: accept_save + start the worker, extracted from handlers/save_intake.py
    ├── reservations.py         # + writes the DOCRES#<document_id> marker when a reservation is created
    ├── config.py               # + event_defaults(): order/received date, warehouse, order type, or the missing names
    └── errors.py               # + EVENT_IGNORED, EVENT_INVALID, DEFAULTS_NOT_CONFIGURED
tests/
├── fixtures/sample_event.json  # NEW: the example event as valid JSON
├── tools/make_sample_event.py  # NEW: repairs sample_events.json into valid JSON
├── fakes.py                    # + event builder helper
└── unit/ test_eventdoc.py, test_event_intake.py, test_eventstore.py, test_idp_summary.py, test_template_event.py
docs/EVENT_INTAKE.md            # NEW: set-up, outcomes, what the sample document does and why
```

**Structure Decision**: one new handler orchestrates, three small modules own the pieces (parsing, once-only record, save start), and `idp.py` gains a read-once split so the summary and the PO details come from one IDP read. The existing `run_reserve`, `idp.po_details` rules and the worker are reused untouched in behaviour.

## Complexity Tracking

| Item | Why needed | Simpler alternative rejected because |
|---|---|---|
| A separate `DOCRES#` marker item written by every reservation | FR-006b: an event must see a reservation made through the request workflow | Scanning the table for a document ID costs a full scan and is not atomic |
| `EVTDOC#` item with a conditional claim | Two simultaneous deliveries must not both reserve; async invocations are at-least-once | Relying on `request_ref` alone covers repeats of this path but not the request workflow |
| `read_document()` split in `idp.py` | The summary must be returned even when the PO details are refused (FR-006a) without reading the IDP three times | Calling `po_details` and a separate summary read doubles the IDP traffic |
| `savestart.py` extracted from the save-intake handler | The event path and the API path must start a save identically | Copying the code would let the two drift |
