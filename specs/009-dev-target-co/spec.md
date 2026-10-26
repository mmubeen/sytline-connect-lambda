# Feature Specification: Development Mode Against a Pre-Created Customer Order

**Feature Branch**: `009-dev-target-co`

**Created**: 2026-10-26

**Status**: Draft

**Input**: User description: "Modify to target a pre-created CO_Number similar to Job_Number that can be overwritten in the ERP System and is not blocked by the entry in the dynamodb job tracker. This is to be able to work in a development mode in the production system without causing impact to the ERP system."

## Background

Today the test setting `CO_NUMBER` makes every reservation reuse one customer order (CO) number instead of asking the ERP for a new one, and `JOB_NUMBER` does the same for the job number. The save then **inserts** the CO header, lines and jobs, so the ERP refuses it with "The Order entered already exists" as soon as that CO exists, and the job tracker (the reservation records the integration keeps per document and per request) also refuses to start a second reservation or save for a document it has already handled. A developer working in the **production** ERP therefore either burns a new CO and job number on every attempt, leaving orphan orders for the ERP owner to clean up, or cannot repeat a test at all.

This feature adds a **development mode**: the deployer names a customer order that was created in the ERP beforehand for testing. Every reservation and save then targets that one order, **overwrites** what is in it instead of creating a new one, and is never blocked by what the job tracker remembers about earlier runs of the same document. No new CO or job number is consumed and no new order is created, so repeated development runs leave the production ERP as it was apart from the one designated order.

## Clarifications

### Session 2026-10-26

- Q: If the target belongs to a different customer than the document resolves to, overwrite or refuse? → A: Refuse (nothing written), so a mistyped order number cannot rewrite a real customer's order.
- Q: Besides the order and its lines, is the pre-created job also overwritten when a job number is configured? → A: Yes (assumed default). When `JOB_NUMBER` is set and that job exists, the save overwrites it; when it does not exist, the save creates it as today.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Repeat a save against the same pre-created order (Priority: P1)

A developer has a customer order that was created in the production ERP for testing, and gives its number to the deployment as the development target. They send a document, the integration reserves and saves it **into that order**: the order's header and lines now hold the document's values, replacing whatever an earlier run wrote. They send the same document again (or a corrected one) and the order is overwritten again, with no "order already exists" refusal and no new order or number appearing in the ERP.

**Why this priority**: it is the whole point of the feature; without overwrite there is no repeatable development run, and every attempt leaves residue in production.

**Independent Test**: set the development target to an existing order, send a document twice with different values; after each run, the ERP holds exactly one order with that number carrying the latest values, and the order count and the CO/job number counters are unchanged.

**Acceptance Scenarios**:

1. **Given** a development target that exists in the ERP, **When** a document is saved, **Then** the target's header and lines are replaced with the document's values and no other order is created or changed.
2. **Given** the same document is saved again with changed values, **When** the save runs, **Then** the target shows the new values and the response reports the order as overwritten, not created.
3. **Given** a development target is set, **When** a reservation is made, **Then** no CO number is requested from the ERP, and, if a development job number is also set, no job number is requested either.
4. **Given** the target has fewer lines than the document, **When** the save runs, **Then** the missing lines are added to the target; given it has more, **Then** the surplus lines are left as they are and reported.

---

### User Story 2 - The job tracker never blocks a development run (Priority: P1)

A developer re-sends a document that already has a reservation or a finished save recorded in the job tracker. In development mode the tracker's earlier entry does not refuse, replay or short-circuit the run: the document is processed again against the development target. The tracker still records what happened so the run can be seen afterwards, and the entry is marked as a development run.

**Why this priority**: equally blocking; the tracker's "already reserved / already saved / uncertain, do not retry" protections are right for real orders and wrong for a repeatable test.

**Independent Test**: save a document in development mode, then send the same document and request again; the second run is processed (not reported as already processed) and ends with its own outcome.

**Acceptance Scenarios**:

1. **Given** the tracker holds a finished save for the document, **When** the document is sent again in development mode, **Then** it is processed again against the target.
2. **Given** the tracker holds an earlier run for the document that ended as uncertain or failed, **When** it is sent again in development mode, **Then** it is not held back by that entry.
3. **Given** development mode is **not** set, **When** the same document is sent again, **Then** the tracker's existing protections apply exactly as today.

---

### User Story 3 - Development mode cannot touch any other order (Priority: P2)

The deployer and the ERP owner need to know that development mode changes exactly one order. If the designated order does not exist, or the save would touch any other order, nothing is written and the response says why. The mode is visible: the deployment warns that it is set, and every response and log line from a development run says so.

**Why this priority**: the work happens in the production system; the safety of every other order depends on this.

**Independent Test**: set the target to a number that does not exist: the save is refused and nothing is written; set it to an existing order and inspect the requests made: they name only that order.

**Acceptance Scenarios**:

1. **Given** the target does not exist in the ERP, **When** a reservation or save is attempted, **Then** it is refused before anything is written, naming the missing order.
2. **Given** development mode is set, **When** any run is made, **Then** the response and the log mark it as a development run and name the target.
3. **Given** development mode is set, **When** the deployment is made, **Then** the deployer sees a warning that it is set and which order it targets.
4. **Given** the setting is cleared, **When** the next document is processed, **Then** the integration behaves as it does today (new numbers, inserts, tracker protections).

---

### Edge Cases

- The target exists but belongs to a different customer than the one the document resolves to: the save is refused, nothing is written, and the response names both customers.
- The ERP refuses the overwrite partway (header replaced, a line refused): the order is reported as needing reconciliation, as for any partial write; nothing is deleted and nothing is retried automatically.
- A document with several lines and a single configured job number: all lines would use the same job number; this is as documented for `JOB_NUMBER` today and the test is expected to use single-line documents.
- Two development runs for different documents overlap on the same target: the later write wins; no locking is attempted.
- The development setting is left on in a deployment that then receives real documents: every one overwrites the target. The deployment warning and the marking of responses are the only protection.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The deployer MUST be able to name a development target customer order; when it is empty, behaviour is exactly as today.
- **FR-002**: With a target set, reservation MUST use the target as the order's number and MUST NOT request a CO number from the ERP; with a development job number also set, it MUST NOT request a job number either.
- **FR-003**: With a target set, the save MUST overwrite the target's header and its lines (and the job, when a development job number is set and the job exists) with the document's values instead of inserting new rows, and MUST NOT be refused because the order already exists.
- **FR-004**: The save MUST add lines (and the job) that do not yet exist on the target, and MUST leave and report surplus lines the document does not have.
- **FR-005**: Before writing anything, the save MUST confirm the target exists in the ERP and refuse, writing nothing, if it does not.
- **FR-005a**: The save MUST refuse, writing nothing, when the target's customer differs from the customer the document resolves to.
- **FR-006**: The save MUST NOT create, change or delete any order other than the target.
- **FR-007**: With a target set, the job tracker's entries for the document and the request (already reserved, already saved, uncertain outcome, finished) MUST NOT refuse, replay or short-circuit a reservation or save; a run MUST always proceed against the target.
- **FR-008**: The tracker MUST still record each development run and its outcome, marked as a development run, so it can be inspected afterwards.
- **FR-009**: Every response, log line and tracker entry of a development run MUST say it is a development run and name the target.
- **FR-010**: The deployment MUST warn while a target is set, naming it; the setting MUST be validated (digits, the ERP's order-number length) before deployment.
- **FR-011**: A partial overwrite MUST end as "needs reconciliation" with what may have been written; nothing is deleted and nothing is retried automatically.
- **FR-012**: Live ERP writes MUST remain off unless explicitly enabled; development mode does not enable them.
- **FR-013**: The mock environment and the tests MUST cover overwrite of an existing order, creation of missing lines, refusal of a missing target, and bypass of the tracker.

### Key Entities

- **Development target**: the pre-created customer order (and optionally job) the deployment is pointed at; exists in the ERP before any run; the only order a development run may change.
- **Development run**: one reservation or save made while a target is set; marked as such everywhere it is recorded.
- **Job tracker entry**: the integration's record of a document's reservation or save; in development mode it is written but never consulted to refuse a run.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A developer can send the same document 10 times in a row against one target and every run completes with the target holding the latest values, with no "order already exists" refusal.
- **SC-002**: After any number of development runs, the count of customer orders in the ERP and the next CO and job numbers are unchanged.
- **SC-003**: In 100% of development runs the ERP orders changed are limited to the target.
- **SC-004**: A development run is never reported as "already processed" because of an earlier tracker entry; with the setting cleared, the tracker's protections behave as before in 100% of the existing tests.
- **SC-005**: A missing target is reported before any write in 100% of attempts.
- **SC-006**: A reader of any response or log line can tell in one look that it was a development run and which order it targeted.

## Assumptions

- The target order is created and kept by the ERP owner or developer outside this integration; the integration never creates it.
- "Overwritten" means the document's values replace the order's current header and line values; it does not delete lines.
- The mode is a deployment setting, as `CO_NUMBER` and `JOB_NUMBER` are today (it supersedes `CO_NUMBER`); there is no per-request switch, so a caller cannot turn it on.
- Tracker bypass applies to the reservation and save workflows and the document event intake; the tracker's other duties (recording, cleanup of stale entries) continue.
- The existing rule that live ERP writes are off by default stays, and the job tracker is not modified or deleted by this feature, only not consulted for refusals.
- Dependencies: the ERP accepts updates to an existing order's header and lines through the same web service; this is unverified against the real ERP.
