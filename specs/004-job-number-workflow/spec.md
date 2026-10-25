# Feature Specification: Job Number Reservation and Save Workflow

**Feature Branch**: `004-job-number-workflow`

**Created**: 2026-10-21

**Status**: Draft

**Input**: User description: "create a workflow to get the jobnumber from ERP. use the flow implemented in the PO OCR demo program (COMPLETE_OCR) to save the information to ERP after getting the jobnumber"

## Background

In the PO OCR demo program, a job number is never looked up: SyteLine's own numbering routine **reserves the next free job number**, and that number is then threaded through everything that is saved afterwards. The demo's order is: identify the customer, reserve the customer order (CO) number, save the CO header, reserve one job number per PO line up front, save each CO line carrying its job number as the link to the job, set the line's teardown date, save the job for that line, and record per line whether it worked. A reserved number is consumed even if a later save fails, which leaves a gap; that matches how SyteLine behaves for its own users.

Feature 001 already saves a confirmed order in one step and obtains its numbers inside that step. This feature adds a **two-stage workflow** built on the demo's flow: the caller first gets the job number(s) from the ERP, sees them, and then saves the order information using those numbers. The existing one-step upload is unchanged.

## Clarifications

### Session 2026-10-21 (document source)

- Q: Where do the customer, PO number and lines come from? → A: From the document's inference result in the IDP (intelligent document processing) solution, fetched through the IDP SDK by **document ID**. The caller sends the document ID, not the PO details, and the PO details are **not stored** in the reservation record (only the document ID, a fingerprint and the per-line outcomes).
- Q: Which results may be saved? → A: The document must have finished processing. If every field the save uses meets its confidence threshold, the inference result is used as it is. If any such field is below its threshold, the result is used only once a person has reviewed it; until then reserving and saving are refused.
- Q: Does the document ID go to reserve, to save, or both? → A: Both. Reserve fetches the result, resolves the customer and binds the document (customer, PO number, line count) to the reservation; save names the same document, fetches the result again and refuses it if it no longer matches.
- Pending: the IDP document class and the field names for customer, PO number and line items are to be supplied; until they are, the mapping is configurable and its names are unconfirmed.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Get job numbers from the ERP (Priority: P1)

An operator or upstream system has a confirmed PO and needs the job number for each line before anything else is saved (for example to name the filed PO copy after the job). They ask the ERP workflow to reserve one job number per PO line and receive the numbers, each tied to its line, together with the customer order number they belong to.

**Why this priority**: the job number is the key the rest of the process depends on; without a dependable way to obtain it nothing else can proceed.

**Independent Test**: Request reservations for a PO with three lines against a simulated ERP; confirm three distinct job numbers and one CO number are returned, each job number mapped to its line, and nothing else is written to the ERP.

**Acceptance Scenarios**:

1. **Given** a confirmed PO whose customer matches exactly one ERP customer, **When** reservation is requested, **Then** one CO number and one job number per line are returned, in line order, and no order, line or job record is created.
2. **Given** two reservation requests at the same time, **When** both complete, **Then** no job number or CO number appears in both.
3. **Given** the customer matches no ERP customer or more than one, **When** reservation is requested, **Then** it is refused with a clear message and no number is reserved.
4. **Given** live ERP writes are not enabled, **When** reservation is requested, **Then** no number is reserved and a clear "live writes disabled" result is returned.

---

### User Story 2 - Save the order information using the reserved numbers (Priority: P1)

After receiving the reservation, the caller asks for the order information to be saved. The workflow follows the demo's order: save the CO header, then for each line save the CO line carrying its job number, set the line's teardown date, and save the job for that line. Each line's outcome is reported with its job number, CO line number and item.

**Why this priority**: this is the point of the workflow; reserving numbers has no value until the information is saved against them.

**Independent Test**: Reserve numbers for a two-line PO, then save it; confirm in a simulated ERP that the header, two lines (each referencing its job number) and two jobs (each linked back to its line) exist and that the response lists each job number with its line and item.

**Acceptance Scenarios**:

1. **Given** a valid reservation for a PO, **When** the save is requested with the same PO details, **Then** the CO header, each line, each line's teardown date and each line's job are saved, and every line is reported as saved with its job number, CO line number and item.
2. **Given** the save finishes, **When** the records are read back from the ERP, **Then** every saved line references its job number and every saved job references its CO and line.
3. **Given** setting a line's teardown date fails but the line and job saved, **When** the save finishes, **Then** the line is reported saved with a warning, and the failure does not stop the job from being saved.
4. **Given** one line's save fails after earlier lines saved, **When** the save finishes, **Then** the result states per line what was saved and what failed, and nothing already saved is deleted.

---

### User Story 3 - Safe handling of reservations and failures (Priority: P2)

A reservation can only be used for the PO it was made for, only once, and only by the caller that made it. Reserved numbers that are never used, or whose save failed, stay consumed and are reported as unused or failed so a person can account for the gap.

**Why this priority**: it prevents duplicate or crossed-over records in the live ERP, but the happy path works without it.

**Independent Test**: Try to save with a reservation twice, with different PO details, and as a different caller; confirm each attempt is refused and nothing new is written.

**Acceptance Scenarios**:

1. **Given** a reservation already used for a successful save, **When** a second save uses it, **Then** it is refused and nothing is written.
2. **Given** a reservation made for one PO, **When** a save presents a different PO (different customer, PO number or line count), **Then** it is refused with a clear message and nothing is written.
3. **Given** a save that timed out or whose outcome is unknown, **When** it is requested again, **Then** it is not replayed; it is marked as needing manual reconciliation until a person resolves it.
4. **Given** a reservation whose save failed before anything was written, **When** the caller asks about it, **Then** its numbers are reported as consumed and unused, with the reason.

---

### User Story 4 - Look up what happened (Priority: P3)

The caller can ask for the state of a reservation and its save at any time: reserved, saving, saved, partly saved, failed or needs reconciliation, with the numbers and per-line results.

**Why this priority**: operators need to trace what reached the ERP, but this builds on the first two stories.

**Independent Test**: Reserve, save, then query; confirm the state and per-line results match what was saved, and that a reservation with no save shows as reserved only.

**Acceptance Scenarios**:

1. **Given** any reservation, **When** its status is requested, **Then** it returns the state, CO number, job numbers with lines, and any per-line failures or warnings.

---

### Edge Cases

- ERP unreachable or timing out during reservation: report that clearly; if the outcome is unknown (the number may have been reserved), say so rather than reserving a second set silently.
- A PO with a single line, or with many lines: one job number per line in every case; the request is refused if it exceeds the maximum lines per PO.
- Customer match is only low-confidence or ambiguous (two close candidates): refuse and write nothing, never book against a guessed customer.
- Item on a line not found in SyteLine: that line fails with a clear message; earlier saved lines are reported and kept.
- Reservation requested but never saved: numbers stay consumed (a gap) and are listed as unused; they are never handed to another request.
- Save requested after a long delay: the reservation is still valid, because the ERP never takes reserved numbers back.
- Caller sends the same save twice (retry): no duplicate records are created.
- Items needing special text (long descriptions, non-ASCII characters) are saved without corruption or are refused with a clear message.

## Requirements *(mandatory)*

### Functional Requirements

*Revision 2026-10-21 (document source): FR-018 to FR-022 change where the PO details come from; FR-001, FR-003, FR-004 and FR-009 read "the PO details obtained from the document" wherever they say "the PO" or "the supplied details", and reserve and save requests carry a document ID instead of customer, PO number and line details.*

- **FR-001**: The system MUST offer a **reserve** step that, for a confirmed PO, resolves the customer and obtains from the ERP one customer order number and one job number per PO line, using the ERP's own numbering so that numbers never collide with other users or requests.
- **FR-002**: The reserve step MUST return the CO number and the job numbers in line order, each tied to its line, and MUST NOT create any order, line or job record.
- **FR-003**: The reserve step MUST resolve the customer from the PO's identifying details against ERP customer data and MUST refuse, before reserving any number, when there is no match, more than one match, or a match that is not confident enough.
- **FR-004**: The system MUST offer a **save** step that takes a reservation and the PO details and, in this order, saves the CO header, then for each line saves the CO line with its job number as the link to the job, sets the line's teardown date, and saves the job linked back to the CO and line.
- **FR-005**: The save step MUST use exactly the numbers that were reserved, in line order, and MUST NOT obtain further numbers.
- **FR-006**: The save step MUST read back the saved records and report each line as saved only when the CO line, the job and the link between them are confirmed.
- **FR-007**: A failure to set a line's teardown date MUST be reported as a warning on that line and MUST NOT prevent that line's job from being saved.
- **FR-008**: The save step MUST report per line the job number, CO line number, item and result, and MUST report a failure on one line without deleting or undoing anything already saved.
- **FR-009**: A reservation MUST be usable for exactly one save, only for the PO it was made for (same customer, PO number and number of lines) and only by the caller that made it; any other use MUST be refused with nothing written.
- **FR-010**: The system MUST NOT replay or retry a save whose outcome is unknown; it MUST be marked as needing manual reconciliation, as in the existing upload, until a person resolves it.
- **FR-011**: Reserved numbers that are never used, or whose save failed, MUST stay consumed (never reissued) and MUST be reported as unused or failed.
- **FR-012**: The system MUST provide a status request returning the state of a reservation and its save: reserved, saving, saved, partly saved, failed, or needs reconciliation, with the numbers and per-line results and warnings.
- **FR-013**: The system MUST validate the request before contacting the ERP and report all validation problems together.
- **FR-014**: The system MUST NOT reserve a number or write to the ERP unless live ERP writes are explicitly enabled, because reserving a number is itself a change in the ERP.
- **FR-015**: The system MUST read and write ERP data only through the ERP web service, never through a direct database connection, and MUST keep credentials out of logs and responses.
- **FR-016**: The system MUST accept only requests from approved cloud identities, MUST require an identifier of who confirmed the PO, and MUST record every reserve and save (identifiers, outcome, duration) so what reached the ERP can be traced.
- **FR-017**: The existing one-step order upload, notes and connectivity actions MUST keep working unchanged.

- **FR-018**: The reserve and save requests MUST identify the PO by an IDP **document ID**. The system MUST obtain the customer details, PO number and lines from that document's inference result through the IDP SDK; callers MUST NOT supply them.
- **FR-019**: The system MUST refuse to reserve or save, writing nothing, when the document does not exist, has not finished processing, is missing a field the save needs, or has a needed field below its confidence threshold that has not been reviewed. The refusal MUST name the document state or the fields concerned.
- **FR-020**: Reserve MUST bind the document to the reservation (document ID and a fingerprint of the customer, PO number and line count). Save MUST name the same document, obtain its result again, and refuse, writing nothing, if the document or its result no longer matches what was reserved.
- **FR-021**: The system MUST NOT store the PO details (customer, PO number, lines) in its own records; it MUST store only the document ID, the fingerprint, the reserved numbers and the per-line results.
- **FR-022**: An unavailable or failing IDP source MUST be reported clearly and MUST NOT cause any number to be reserved or any record to be written.

### Key Entities

- **Reservation**: The set of numbers obtained from the ERP for one PO: one CO number and one job number per line, tied to the PO, the caller and the time. It has a state (reserved, saving, saved, partly saved, failed, needs reconciliation) and can be used for one save.
- **Job Number**: The ERP's work order number for one CO line, reserved before the line and job are saved and used as the link between them.
- **Line Result**: For one PO line: job number, CO line number, item, saved or failed, and any warning (such as the teardown date).
- **Document Result**: The inference result of one IDP document (its status, extracted fields and per-field confidence), identified by the document ID. It is the only source of the PO details.
- **PO Details**: The purchase order data taken from the document result: customer details, PO number, received date and the lines (item, quantity, serial and engine details, packing).
- **Customer Match**: The result of resolving the PO's customer details to exactly one ERP customer.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For a PO of up to 20 lines, a caller receives its CO number and all job numbers in under 30 seconds.
- **SC-002**: In concurrent testing with at least 20 simultaneous reservations, 100% of issued job numbers and CO numbers are distinct.
- **SC-003**: Across test runs against a simulated ERP, 100% of lines reported as saved have a CO line, a job and a confirmed link between them in the ERP.
- **SC-004**: 100% of attempts to reuse a reservation, to use it for a different PO, or to use it as a different caller are refused with nothing written.
- **SC-005**: With live writes disabled, 0 numbers are reserved and 0 records are written, in every test.
- **SC-006**: A failed or uncertain save never causes deletion or replay: in failure-injection tests, 0 records are removed and 0 saves are repeated automatically.
- **SC-007**: An operator can tell from the status of any reservation, without database access, which numbers were used, which were consumed without use, and why.

## Assumptions

- "Get the job number from ERP" means reserving the next free job number from the ERP, as the demo does, not looking up an existing job. Looking up the job number of an already-saved order is out of scope.
- The save step follows the demo's order and field mapping (CO header, CO line carrying the job number, teardown date, job record) and uses this project's existing rules: ERP access through the web service only, confirmed-only requests, per-request safety states, no automatic delete or retry.
- The caller supplies only the document ID (and who is confirming); the PO details come from the IDP result, and the system keeps only what it needs to check that the save is for the document that was reserved.
- A result that meets its confidence thresholds is trusted as it is; below a threshold it needs a person's review in the IDP solution first. How "reviewed" is recognised is to be confirmed against the IDP solution; until then a below-threshold field is refused.
- Reserved numbers do not expire, because the ERP never reclaims them; unused numbers are accounted for, not recycled.
- The demo's last step, filing a copy of the scanned PO in a job folder on a network share, is out of scope: this service has no access to that share. The caller can do it using the returned job numbers.
- The demo's reporting by customer number and per-line tracker are covered by the per-line results and status request, not by a separate audit file.
- The existing one-step upload (feature 001) stays as it is; this workflow reuses its customer matching, safety states and verification rather than replacing them.
