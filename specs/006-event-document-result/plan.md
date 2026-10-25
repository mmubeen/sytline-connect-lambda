# Implementation Plan: Document Result from a Processing-Complete Event

**Branch**: `006-event-document-result` | **Date**: 2026-10-22 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/006-event-document-result/spec.md`

## Summary

Add a second way to start the job-number workflow (feature 004): a new function in the plant deployment that the document-processing service's "Step Functions Execution Status Change" event starts. It (1) parses the event and takes the document ID from the execution `input`/`output` text, ignoring or refusing events that cannot be used, (2) reads the document's current result once through the IDP SDK (never the event's snapshot), builds the scan result summary, (3) when the result is usable and live writes are on, reserves the CO and job numbers with the existing `run_reserve` and accepts the save and starts the existing worker, exactly once per document, and (4) records the outcome so a repeat delivery returns it. No new ERP behaviour: all safeguards are those of features 004 and 005. The IDP SDK usage is the one already in `src/syteline/idp.py`; the SDK documentation was run through the document reviewer (research.md R1).

**Revision 2026-10-22 (one Lambda, `action` decides):** the separate `EventIntakeFunction` is folded into the existing direct-invoke Lambda (`lambda_function.lambda_handler`, `DirectFunction`); the new action `document_event` is the **default** when no `action` is given, `health` must be asked for explicitly, and a bare payload that is not an IDP event is refused with a message naming `"action": "health"` (research.md R11 to R16, spec FR-011 to FR-015).

**Addendum 2026-10-24 (FR-016, SC-009): entry log.** On entry to `lambda_handler` the function writes one JSON line holding the event: a redacted form by
default, the whole event (size-capped) only when `LOG_EVENTS=true`. See research R17..R21 and `contracts/entry-log.md`.

**Addendum 2026-10-25 (FR-006e; feature 004 FR-023): optional job number.** A line item of the document may carry a job number; the reserve step uses it for that line instead of allocating one. See research R22..R26, `data-model.md`, and the changes to both contracts.

**Addendum 2026-10-25 (FR-016 revised): whole event, no cap.** With `LOG_EVENTS=true` the line holds the whole event (header credentials still masked); an event too large for one CloudWatch entry (256 KB) is also written to an S3 bucket the stack creates (only when the setting is on). See research R27..R32; this supersedes the 8000-character cap of R19/R21.

## Technical Context

**Language/Version**: Python 3.13 (Lambda; existing code), SAM/CloudFormation YAML

**Primary Dependencies**: existing only (idp_sdk via `IDP_REPO`, boto3, zeep, python-tds, rapidfuzz); the EventBridge rule is a template resource, no library

**Storage**: the existing jobs table (new item kinds `DOCRES#<document_id>` and `EVTDOC#<document_id>`; no PO details stored, feature 004 FR-021)

**Testing**: pytest with the existing fakes (`FakeIdp`, `FakeSoap`, `FakeJobsTable`, `FakeLambda`, `FakeSqlServer`), a repaired copy of the example event as a fixture, template tests

**Target Platform**: AWS GovCloud us-gov-east-1; plant VPC deployment only

**Project Type**: serverless service, extension of an existing project

**Performance Goals**: summary under 10 s (SC-001); a 25-line event run to "save accepted" under 60 s (SC-007; the save itself is the existing asynchronous worker)

**Constraints**: read-only until a result is usable and live writes are on; once per document; no automatic retry of the function (a replayed reservation burns numbers); logs without field values (FR-009); the event and the IDP service must be in the same account and region as the plant stack (EventBridge rule limitation)

**Scale/Scope**: tens of documents per day, up to 25 lines each

## Constitution Check

`.specify/memory/constitution.md` is still the unfilled template: no ratified gates. The spec's own rules and the standing rules of features 001 to 005 were applied; none is relaxed. Reserving and writing stay behind the live-writes switch.

**Entry-log gate (added):** the standing rule that credentials are never logged is stronger than the opt-in: header values such as `Authorization` and
`X-Amz-Security-Token` stay masked even when `LOG_EVENTS=true`. Pass: R19.

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
├── template.yaml               # + IdpStateMachineArn parameter, rule (only when the ARN is given); DirectFunction gains the intake's settings and permissions (no separate function), no retries
├── Makefile                    # (revision: no separate EventIntakeFunction target)
├── deploy.sh                   # + IDP_STATE_MACHINE_ARN (plant branch only, always passed; format check)
├── deploy.env.example          # + IDP_STATE_MACHINE_ARN in the PLANT ONLY block
├── lambda_function.py          # revision: action dispatch; default `document_event`; strict refusal of a bare non-event payload; `ok` and log for the new action
├── handlers/
│   └── event_intake.py         # NEW: `process(payload, context)` = parse -> read once -> summary -> reserve -> accept save -> outcome
└── syteline/
    ├── eventdoc.py             # NEW: event parsing (document ID, status, execution, event date), refusals; `is_status_change()`; `unwrap()` of an `event` key
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
└── unit/ test_lambda_actions.py (NEW: default action, strict refusal), test_eventdoc.py, test_event_intake.py, test_eventstore.py, test_idp_summary.py, test_template_event.py
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

## Addendum: files for the entry log (FR-016)

```text
src/syteline/eventlog.py        # NEW  redact(), render(), log_entry(): never raises
src/lambda_function.py          # one call at the top of lambda_handler
src/template.yaml               # parameter LogEvents (default "false") -> env LOG_EVENTS
src/deploy.sh, deploy.env.example   # optional LOG_EVENTS=true|false, always passed
tests/unit/test_eventlog.py     # NEW  redaction, cap, whole mode, odd inputs
tests/unit/test_entry_log.py    # NEW  one line per invocation, run unchanged, failure swallowed
tests/unit/test_template_single.py, test_deploy_script.py   # the parameter and its pass-through
docs/EVENT_INTAKE.md, docs/DEPLOYMENT.md   # what is logged, how to turn it on, who can read it
```

## Addendum: files for the optional job number (FR-006e, feature 004 FR-023)

```text
src/syteline/idp_fields.py      # + "line.job" in DEFAULT_MAP (optional, text; default name `job_number`, UNCONFIRMED) and in _LINE_KINDS; the line dict gains "job"
src/syteline/validation.py      # fingerprint(): + optional `supplied` ({line: job}); appended only when not empty, so existing fingerprints do not change
src/syteline/idp.py             # _details(): passes the supplied numbers to the fingerprint; a low-confidence job field is "needs review" like any used field
src/syteline/reserve.py         # per line: a supplied job is used as is (no allocate, not in `consumed`); entries carry "source": "document" | "allocated"
src/syteline/reservations.py    # numbers_report(): + "source"; usage for a supplied number that was not saved is `supplied_not_saved`, not `consumed_unused`
tests/unit/                     # test_idp_fields.py (mapping, blank, trimmed), test_reserve.py (mixed lines, all supplied, store failure), test_reservations.py (usage), test_event_intake.py (response shows source), test_idp.py (low confidence on the job field)
docs/EVENT_INTAKE.md            # the optional field, the IDP_FIELD_MAP key `line.job`, and what happens if the ERP rejects the number
```

No template, permission, deploy-script or storage-key change: the supplied number is one more attribute of the existing reservation entries.

## Addendum: files for the uncapped entry log (FR-016 revised, research R27 to R32)

```text
src/syteline/eventlog.py        # no cap, no LOG_EVENTS_MAX_CHARS; MAX_LINE_BYTES (200,000); oversized event -> head that fits + copy to S3 (put_event_copy()) ; failures swallowed
src/template.yaml               # + Number parameter EventCopyRetentionDays (30, 1..365); + condition LogsEventsToS3 (LogEvents == "true");
                                #   + AWS::S3::Bucket EventCopyBucket (condition; Retain; SSE-S3; public access blocked; lifecycle expiry; TLS-only policy);
                                #   + env EVENT_COPY_BUCKET; + s3:PutObject on <bucket>/events/* in the function's policies (condition); + Output (condition)
src/deploy.sh, deploy.env.example   # + EVENT_COPY_RETENTION_DAYS (default 30, 1..365) always passed; the LOG_EVENTS=true notice also names the retained bucket
mock/render_env.py              # physical name of EventCopyBucket (a valid, lowercase S3 name)
mock/edge/seed.py               # create the bucket in Moto when EVENT_COPY_BUCKET is set (reset empties it)
tests/unit/test_eventlog.py     # the cap tests become no-cap + oversize tests; copy ok / copy failed / no bucket
tests/unit/test_entry_log.py    # the oversized event still gives exactly one valid JSON line and never changes the run
tests/unit/test_template_single.py, test_deploy_script.py, mock/test_render_env.py   # the bucket and its conditions, the parameter, the policy statement
docs/EVENT_INTAKE.md, docs/DEPLOYMENT.md   # no cap; the bucket (retention, retained on delete, who can read it); the S3 route the subnets need
```

Constitution check: still no ratified gates. The standing rule that credentials are never logged is kept (masking applies to the log line and to the S3 copy). Feature 007 FR-003 allows the bucket as a supporting, non-network resource.
