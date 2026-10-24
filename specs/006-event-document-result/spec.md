# Feature Specification: Document Result from a Processing-Complete Event

**Feature Branch**: `006-event-document-result`

**Created**: 2026-10-22

**Status**: Draft

**Input**: User description: "lambda is invoked with /Users/mohammed.mubeen/Documents/ca-project/sytline-connect-lambda/sample_events.json as event. Parse json to get documentId and retrieve document result using idp api. use review doc to get the document scan results"

## Background

The document-processing service (the IDP accelerator) announces, for every document it handles, a **processing status change event** (a Step Functions execution status change). `sample_events.json` at the repository root is an example: the event says the processing of document `159112.pdf` **SUCCEEDED**, and carries the document (its identifier `id`, its pages and sections, its state) as text inside the execution's `input` and `output` fields. Today the functions only learn about a document when a caller sends its document ID in a request (feature 004). This feature lets the integration function be **started by that event**: it finds the document ID in the event, reads the document's scan result from the document-processing service, and, when the result is good enough, **reserves the job numbers and saves the order to the ERP automatically** using the job-number workflow of feature 004.

Two facts shape the work:

- The text inside the event is **a snapshot taken while processing ran**: the example's `input` shows the document as `RUNNING` and its `output` as `EVALUATING`, and neither holds the extracted field values. The current result has to be read from the document-processing service.
- `sample_events.json` as saved is **not valid JSON** (single quotes, `None`, spaces inside ARNs and timestamps, and line breaks inside quoted text). A real event is delivered as valid JSON, so the saved file is an illustration that must be repaired before it can drive tests.

## Clarifications

### Session 2026-10-22

- Q: After the scan result is read, what should the function do? → A: Go all the way: read the result, then reserve the CO and job numbers and save the order to the ERP, with no person starting either step. All the safeguards of feature 004 stay: live ERP writes must be on, a result with a field below its confidence threshold is refused until a person has reviewed it, a customer that is not matched confidently is refused, and a document is processed at most once however many times its event is delivered. The scan result summary is still returned.
- Q: What does "make this the default Lambda behaviour" mean? → A: Consolidate: there is one Lambda for the direct actions and the event intake, and its `action` decides what runs. The separate event-intake function is folded into it; the trigger rule (when `IDP_STATE_MACHINE_ARN` is set) targets that one Lambda. The trigger itself stays opt-in; live writes stay off by default.
- Q: How does the IDP status-change event, which has no `action`, reach the intake? → A: The intake is the **default action**: when the payload has no `action`, the action is `document_event`. An explicit `action` always wins (`health`, `add_note`, `get_notes`, `document_event`; an unknown value is refused as today). The event is the payload itself; an `event` key holding it is also accepted.
- Q: What does a payload with no `action` that is not an IDP event (for example `{}` or `{"dry_run": true}`) do now? → A: Strict: it is handed to the intake and refused with `EVENT_INVALID`, and the message says to pass `"action": "health"` for the health check. A health check therefore always needs `"action": "health"`; this supersedes "`action` defaults to health" of features 001 and 003 for this Lambda.

### Session 2026-10-24

- Q: What should the entry log line contain for an incoming event? → A: By default a redacted form of the event as JSON: the bulky parts that can hold document or order content (`detail.input`, `detail.output`, an API request body) are replaced by their size and, for an IDP event, the document ID; everything else is logged as is. Only when an opt-in setting (for example `LOG_EVENTS=true`) is on is the whole event logged, with a size cap. The setting is off by default.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Read the scan result of the document named in the event (Priority: P1)

When the document-processing service finishes a document, the function is started with the status change event. It takes the document ID out of the event, asks the document-processing service for that document's result, and returns a summary of the scan result: the document ID, its state, its class, the number of pages, each extracted field with its value and confidence, and which fields are below their confidence threshold.

**Why this priority**: Everything downstream (job numbers, saving to the ERP) needs the scan result of the right document; reading it from the event alone is wrong, because the event holds only a snapshot.

**Independent Test**: Invoke the function with the (repaired) sample event; with the document-processing service answering for `159112.pdf`, the response names `159112.pdf`, its state, class `purchase_order`, 3 pages and the extracted fields with their confidence, and nothing was written anywhere.

**Acceptance Scenarios**:

1. **Given** the sample event (status SUCCEEDED, document ID `159112.pdf`), **When** the function is invoked with it, **Then** the document-processing service is asked for `159112.pdf` and the response is the summary of that service's current result.
2. **Given** the event's embedded document data differs from the service's current result (for example the event shows `EVALUATING`, the service says `COMPLETED`), **When** the function runs, **Then** the summary shows the service's values, never the event's.
3. **Given** a field whose confidence is below its threshold in the result, **When** the summary is built, **Then** that field is listed as below threshold (the example event has `line_items[0].material_code`, 0.55 against 0.8).

---

### User Story 2 - Reserve the job numbers and save the order automatically (Priority: P2)

When the result is usable (the document is finished, every field the save needs is present and meets its confidence threshold, or has been reviewed by a person), the function goes on without anyone asking: it reserves one CO number and one job number per line from the ERP and saves the order, exactly as the job-number workflow of feature 004 does. The response adds the outcome of each step.

**Why this priority**: it removes the manual step of sending the document ID; but it writes to the ERP, so it comes after the read-only part (US1) is proven.

**Independent Test**: With a simulated document-processing service holding a fully confident 3-line PO and a simulated ERP, invoke with the event and live writes on; one CO number and three job numbers are reserved, the order is saved, and the response lists the numbers and per-line results. Invoke again with the same event: nothing new is reserved or saved.

**Acceptance Scenarios**:

1. **Given** a usable result, a confident customer match and live writes on, **When** the event arrives, **Then** one CO number and one job number per line are reserved and the order is saved, and the response gives the numbers and the per-line results.
2. **Given** the example event, whose document has a field below its confidence threshold (`line_items[0].material_code`, 0.55 against 0.8) and no review, **When** the event arrives, **Then** nothing is reserved or saved, and the response says the document needs review and names the field.
3. **Given** live writes are off, **When** a usable event arrives, **Then** nothing is reserved or saved and the response says why; the scan result summary is still returned.
4. **Given** the same event is delivered a second time (or twice at once), **When** it is processed, **Then** no second reservation or save is made and the response points to the first outcome.
5. **Given** a save whose outcome is uncertain, **When** the event is delivered again, **Then** it is not replayed; the response says the document needs manual reconciliation.

---

### User Story 3 - Ignore events that cannot be used, clearly (Priority: P3)

Events that are not a finished-processing event, or that carry no document ID, must not cause a lookup or any other work. The function answers with the reason.

**Why this priority**: the event source emits an event for every status change of every execution, so unusable events are routine and must be harmless.

**Independent Test**: Invoke with events whose status is `RUNNING`, `FAILED`, `TIMED_OUT` or `ABORTED`, with an event from another source, and with a SUCCEEDED event with no document ID; each answers with a reason and neither the document-processing service nor the ERP is contacted.

**Acceptance Scenarios**:

1. **Given** an event whose status is not `SUCCEEDED`, **When** the function is invoked, **Then** it answers "ignored" with the status and contacts nothing.
2. **Given** a SUCCEEDED event whose `input` and `output` hold no document ID, or text that is not valid JSON, **When** invoked, **Then** it answers with a refusal naming what is missing and contacts nothing.
3. **Given** an `input` document ID that differs from the `output` document ID, **When** invoked, **Then** it refuses and contacts nothing.
4. **Given** a payload with no `action` that is not an IDP event (for example `{}`), **When** invoked, **Then** it is refused as `EVENT_INVALID` with a message that says to pass `"action": "health"` for the health check, and nothing is contacted.
5. **Given** the payload `{"action": "health"}`, **When** invoked, **Then** the health check runs exactly as before.

---

### User Story 4 - Failures are reported, not hidden (Priority: P4)

If the document does not exist, has not finished, or the document-processing service or the ERP is unavailable, the function says so with the same reasons the request-based workflow uses.

**Why this priority**: the event can arrive before the result is readable, or while a service is down; the caller must be able to tell "try again later" from "nothing to do".

**Independent Test**: With the services answering "not found", "still processing" and "unavailable" in turn, each gives a distinct, named outcome; nothing is reserved or saved.

**Acceptance Scenarios**:

1. **Given** the service says the document does not exist, **When** the function runs, **Then** the response names the document ID and says it was not found.
2. **Given** the service says the document is not finished, **When** the function runs, **Then** the response gives its state and says it is not yet readable.
3. **Given** the document-processing service is unavailable, **When** the function runs, **Then** the response says so and nothing is reserved or saved.
4. **Given** the ERP is unreachable when the numbers are reserved, **When** the function runs, **Then** the response says so and no order is saved.

---

### Edge Cases

- The same event is delivered more than once, or twice at the same moment: the document is reserved and saved at most once; every later delivery returns the first outcome and the same scan result summary.
- The document was already reserved or saved through the request-based workflow (feature 004): the event does not start a second reservation; the response says so.
- The event arrives as a text string rather than an object, or with the execution `input` and `output` as nested JSON text (as in the example): both forms are read.
- The `output` is absent (an execution that produced none) but the `input` has the document ID: the `input` is used.
- A document ID with characters outside the usual file-name set, or an empty one: refused as not a document ID, nothing contacted.
- The result is very large (many pages and fields): the summary lists fields and counts but not page images, raw text or other bulk content.
- Event bodies contain customer data (names, addresses, part numbers): by default the logs keep only the document ID, counts and the redacted entry line (FR-016); the whole event is logged only when the opt-in setting is on.
- The event is larger than the log cap, or is not JSON-serialisable (bytes, a string, `None`): the entry line says so and shows what it can; it never raises and never changes the run.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The function MUST accept a processing status change event (source `aws.states`, detail type "Step Functions Execution Status Change") as its invocation payload, as delivered by the event bus, as an object or as JSON text.
- **FR-002**: The function MUST act only on an event whose status is `SUCCEEDED`. For any other status, or an event from another source or of another type, it MUST answer that the event was ignored, with the reason, and MUST NOT contact the document-processing service or the ERP.
- **FR-003**: The function MUST take the document ID from the document in the execution `input` (the `document.id` of the JSON text in `detail.input`) and, if the `input` has none, from `detail.output`. When both hold an ID and they differ, or the text is not valid JSON, or there is no ID, it MUST refuse, naming the cause, and contact nothing.
- **FR-004**: The function MUST read the document's current result from the document-processing service for that ID and MUST NOT use any value embedded in the event as the result (only the document ID is taken from the event).
- **FR-005**: The response MUST contain a summary of the scan result: the document ID, the document's state, its class, the number of pages, each extracted field with its value and confidence, the fields below their confidence threshold, and the number of processing issues. It MUST NOT contain page images, raw page text, credentials or other bulk content.
- **FR-006**: When the result is usable (finished, every field the save needs present, every such field at or above its confidence threshold or reviewed by a person, as in feature 004 FR-019), the function MUST reserve one CO number and one job number per line and save the order to the ERP through the job-number workflow of feature 004, with no person starting either step. All rules of that workflow apply unchanged, including the confident customer match, single-use reservations, no replay of an uncertain save, and the live-writes gate.
- **FR-006a**: When the result is not usable (a field below its threshold and not reviewed, a missing field, or no confident customer match) or live ERP writes are off, the function MUST NOT reserve or write anything, MUST still return the scan result summary, and MUST say in the response why nothing was done and, for a threshold problem, name the fields.
- **FR-006b**: A document MUST be reserved and saved at most once however many times, or how concurrently, its event is delivered. A later delivery MUST NOT reserve or save again and MUST return the first outcome; a document already reserved or saved through the request-based workflow counts as the first outcome.
- **FR-006c**: Every event-started reservation and save MUST be recorded as made by the automatic event intake, identified by the document ID and the execution identifier from the event, instead of by a person who confirmed the PO.
- **FR-006d**: The values the save needs that are not in the document (order date, received date, warehouse, order type) MUST come from configuration: the dates default to the date of the event, the warehouse and order type to the configured defaults; when a needed default is not configured the function MUST refuse before reserving anything and name it.
- **FR-007**: A document that does not exist, has not finished, or whose result cannot be read, and an unavailable document-processing service, MUST each produce a distinct, named refusal (the same codes as the request-based workflow, feature 004 FR-019 and FR-022), with no summary and nothing reserved or written. An unavailable ERP MUST be reported with the scan result summary and the step that failed.
- **FR-008**: Reading the result MUST have no side effect: the same event delivered when nothing may be reserved (ignored, refused or live writes off) MUST give the same summary every time and MUST NOT create, change or delete anything.
- **FR-009**: The log of a run MUST contain the document ID, the event status and the outcome, and the number of fields and of fields below threshold, the reserved numbers and the step outcomes, but not field values, customer names, addresses or part numbers.
- **FR-010**: A usable copy of the example event (valid JSON) MUST be kept as a test fixture, and the saved `sample_events.json` MUST be either repaired to valid JSON or documented as an illustration only.

- **FR-011**: The direct-invoke Lambda and the event intake MUST be one Lambda in the plant deployment, and its `action` MUST decide what runs: `health`, `add_note`, `get_notes` and the new `document_event`. The separate event-intake function MUST NOT remain; the trigger rule, when `IDP_STATE_MACHINE_ARN` is set, MUST target this Lambda.
- **FR-012**: When the payload has no `action`, the action MUST be `document_event`. An explicit `action` MUST always win; an unknown `action` MUST be refused as today. The event MUST be accepted as the payload itself or under an `event` key.
- **FR-013**: A payload with no `action` that is not an IDP processing status change event MUST be refused with `EVENT_INVALID` and a message that names `"action": "health"`; it MUST NOT run a health check or contact anything. A health check MUST always require `"action": "health"` (with its existing `dry_run` and `diagnose` options).
- **FR-014**: In the stand-alone deployment, which has no document source and no reservation store, the `document_event` action (explicit or by default) MUST be refused with a clear message that it is not available in that deployment, and MUST NOT contact anything.
- **FR-015**: The one Lambda MUST carry the permissions and settings of both roles (the connectivity actions and the intake) in the plant deployment, and the connectivity actions MUST behave exactly as before when asked for with their `action`.
- **FR-016**: On entry to the handler, for every invocation (IDP event, API request, direct action, scheduled sweep, the function's own background actions), the function MUST write one log line holding the event as JSON. By default that line is the redacted form: `detail.input`, `detail.output` and an API request body are replaced by their size (and the document ID for an IDP event) and everything else is kept; no field value, customer name or address appears in it. When the opt-in setting is on (off by default; set through the deployment settings) the line holds the whole event, cut to a size cap. Writing the line MUST NOT change the outcome of the run, and any failure to build it MUST be swallowed with a short note.

### Key Entities

- **Processing Status Change Event**: What the document-processing service's event source sends when a document's processing execution changes state: source, detail type, execution identifier, status, and text fields `input` and `output` that hold JSON of the document as it was at that moment.
- **Document ID**: The identifier of the document in the document-processing service (`159112.pdf` in the example), taken from the event's document data.
- **Scan Result Summary**: What the function returns: document ID, state, class, page count, extracted fields (value, confidence, below-threshold flag), processing-issue count.
- **Event Outcome**: The result of acting on the event: ignored (reason), refused (reason), read only (why nothing was reserved), reserved, saved, partly saved, or needs reconciliation, with the reserved numbers and per-line results as in feature 004, tied to the document ID so a repeat delivery finds it.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With the example event, the response names document `159112.pdf` and shows its class, 3 pages and the extracted fields with their confidence, and does so in under 10 seconds when the document-processing service answers normally. Because that document has an unreviewed field below its threshold, the outcome is "needs review" and nothing is reserved or saved.
- **SC-002**: In 100% of tests with a non-SUCCEEDED status, a foreign event, or an event with no or conflicting document ID, the document-processing service is not contacted and the response gives the reason.
- **SC-003**: In every test where the event's embedded data differs from the service's current result, the summary matches the service, never the event.
- **SC-004**: The example's below-threshold field (`line_items[0].material_code`, 0.55 against 0.8) appears in the summary's below-threshold list.
- **SC-005**: Delivering the same event 3 times, one of them at the same moment as another, reserves and saves the document at most once (one CO number and one job number per line in total), and the repeat deliveries return the first outcome.
- **SC-007**: With a fully confident document, live writes on and a simulated ERP, an event results in a saved order with the reserved numbers reported in under 60 seconds for a 25-line PO, with no person involved.
- **SC-008**: With live writes off, or a field below threshold, 100% of tests reserve and write nothing in the ERP.
- **SC-006**: With the event-log setting off (the default), no log line of any test run, including the entry line, contains a field value, customer name or address from the example document.
- **SC-009**: Every invocation test produces exactly one entry line that parses as JSON; with the setting on, the line equals the event (up to the size cap), and with it off the line differs from the event only in the redacted parts.

## Assumptions

- The function is started by the event bus with the event as its payload; setting up that rule and its permission is deployment work for the planning phase, not specified here.
- Reserving a number and saving to the ERP are the steps of feature 004, reused as they are; the unconfirmed ERP and IDP names of that feature (see its notes) must be verified before live writes are turned on, and live writes stay off by default.
- The date of the event stands in for the order and received dates, and the configured defaults for the warehouse and order type, unless the planning phase finds the document carries them.
- The document-processing service, its settings (stack name, region, output bucket, tracking table) and the way the result is read are those already used by the request-based workflow (feature 004); how to use its SDK for reading the result is confirmed from its documentation (the document reviewer) during planning.
- Features 001 and 003 said `action` defaults to `health`; for this Lambda the default is now `document_event` (FR-012, FR-013). Documents, tests and scripts that invoke the health check without an `action` must pass `"action": "health"`.
- The saved `sample_events.json` is illustrative: its quoting is not valid JSON, so a repaired copy is the test fixture (FR-010).
- Only documents of this service's own execution events are expected; the state machine named in the event is not checked against a configured name.
- Thresholds and the "meets threshold" decision come from the document-processing service's own assessment, as in feature 004.

- Logs are kept in the platform's log service and read by operators; turning the opt-in setting on is a deliberate choice that exposes document and order content to whoever can read them.

## Notes

- This feature adds a second way to start the integration with a document (an event, in addition to a request carrying the document ID) and, unlike the request-based workflow, no person confirms the PO: the safeguards are the confidence thresholds, the review requirement, the live-writes switch and the once-per-document rule.
- The example event's document would NOT be saved: it has a field below its threshold (0.55 against 0.8) and `hitl_status` "Review Skipped"; whether a skipped review counts as reviewed is decided by feature 004's rule (a person must have reviewed it), so this document waits for review.
