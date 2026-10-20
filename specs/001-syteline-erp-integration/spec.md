# Feature Specification: SyteLine ERP Integration

**Feature Branch**: `001-syteline-erp-integration`

**Created**: 2026-10-20

**Status**: Draft

**Input**: User description: "update lambda implementation to integrate with ERP sytline"

## Background

Today the Lambda only proves that the SyteLine ERP is reachable and that credentials work (staged DNS / network / service-description / login checks). It does not read or write any ERP business data. This feature turns it into a working integration: upstream systems (e.g. the PO OCR project) can ask it to create customer orders (CO) and jobs in SyteLine, attach notes to those records, and read notes back, while the existing connectivity check is preserved as a health check.

## Clarifications

### Session 2026-10-20

- Q: When a create-order attempt fails or times out after SyteLine may already have accepted some rows, what should the system do? → A: If the failure is a definite rejection before any row was written, a retry is allowed. If any row may exist, mark the request `needs_reconciliation`, make no automatic delete or retry, and report which records may exist. (Source: `docs/Instruction Sheet AWS GovCloud API Gateway to On-Premises Sy.docx`, section 5.)
- Q: How should the system identify the customer when an order comes in? → A: The caller sends customer identifying details (e.g. bill-to name and address), never a customer number. The system resolves the customer in SyteLine's customer address table (`custaddr`) over SQL and refuses the request on no match or an ambiguous match. (Source: instruction sheet, section 4.)
- Q: Should creating an order return a job ID right away, with the upload running in the background and a status call, or should the caller wait for the final result? → A: Asynchronous. `create_order` validates, durably accepts the confirmed order and returns a job ID immediately; a background worker performs the upload; a status request returns the state (`accepted`, `running`, `succeeded`, `failed`, `needs_reconciliation`) plus CO and job numbers when done. (Source: instruction sheet, sections 3–4.)
- Q: Until there is an explicit go-ahead, should anything be written to the live SyteLine ERP, including test orders and field-name discovery? → A: No. There is no test ERP; no live write happens without explicit approval from the ERP owner for that specific test. Until then validation uses mocked and dry-run tests plus read-only network and SQL checks. Any approved live test has a documented reconciliation and cleanup plan. (Source: instruction sheet, section 6.)
- Q: How should callers be authenticated to the order API, and where does `uploaded_by` come from? → A: The calling application signs requests with its AWS identity (IAM). The request carries `uploaded_by` (the confirming operator); the system records it on the upload and logs it with the caller's identity. (Source: instruction sheet, section 3; final method subject to Chromalloy IT approval.)

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Create a customer order and job in SyteLine (Priority: P1)

An upstream system submits order details (customer, line items, quantities, dates). The integration obtains the next customer order and job numbers from SyteLine, creates the records, and returns the resulting identifiers so the upstream system can link them to its source document.

**Why this priority**: Creating CO/job records is the core business value; everything else supports it.

**Independent Test**: Against a simulated ERP, submit a valid order payload and confirm the status request returns the CO and job numbers and the simulated ERP holds the submitted data. A live check is performed only after explicit ERP-owner approval, with a reconciliation and cleanup plan.

**Acceptance Scenarios**:

1. **Given** a valid order request, **When** it is submitted, **Then** a new CO and one job per line are created in SyteLine and their identifiers are returned.
2. **Given** an order request with missing or invalid required data, **When** it is submitted, **Then** nothing is created in SyteLine and the response lists each problem.
3. **Given** the ERP rejects the creation, **When** the request is processed, **Then** the ERP's error message is returned. If nothing was written the request may be retried; if any row may exist the request is marked `needs_reconciliation` and the possibly affected records are listed.

---

### User Story 2 - Attach and read notes on ERP records (Priority: P2)

A user or upstream system adds a note (internal or external) to a CO or job record, and can retrieve all notes recorded against a given record, including recently added ones.

**Why this priority**: Notes carry context (e.g. OCR review comments) but are secondary to record creation.

**Independent Test**: Add an internal note to an existing job, then retrieve notes for that job and see it listed with its creation date.

**Acceptance Scenarios**:

1. **Given** an existing CO or job, **When** a note is submitted, **Then** it is stored against that record and visible in SyteLine.
2. **Given** a record with notes, **When** notes are requested for it, **Then** all notes are returned with type (internal/external) and creation date.
3. **Given** a record identifier that does not exist, **When** a note is added or requested, **Then** a clear "record not found" result is returned and nothing is written.

---

### User Story 3 - Verify ERP connectivity (Priority: P3)

An operator runs the existing health check to find where connectivity breaks (network, service description, login), including the credential-free dry run.

**Why this priority**: Already exists; must keep working as a diagnostic after the integration is added.

**Independent Test**: Run the health check in normal and dry-run modes and compare stage results to current behavior.

**Acceptance Scenarios**:

1. **Given** correct configuration, **When** the health check runs, **Then** it reports the same staged results as before.
2. **Given** a broken network path, **When** it runs, **Then** it stops at the failing stage and names it.

---

### Edge Cases

- ERP unreachable, or a timeout after a possible insert: the outcome is uncertain, so the request is marked `needs_reconciliation` with no automatic delete or retry. If the failure happened before any write (e.g. login failure), return a clear failure and allow a retry.
- Same order submitted twice (retry or duplicate): must not create duplicate CO/job records.
- Concurrent requests: each receives distinct CO and job numbers; no number reuse.
- Very long notes or special characters/non-ASCII text in notes.
- Request exceeds the maximum allowed processing time.
- ERP session expires between login and operation.
- Customer details match no customer, or more than one customer: reject with a clear message and write nothing.
- Required ERP data (item) not found in SyteLine.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST accept requests to create a customer order and associated jobs in SyteLine. It MUST validate the request, durably record it, and return a job ID immediately; the CO and job identifiers are returned through the status request once the upload finishes.
- **FR-002**: The system MUST validate request data before contacting the ERP and report all validation problems together.
- **FR-003**: The system MUST obtain CO and job numbers using SyteLine's own numbering rules so numbers never collide with those issued by other SyteLine users.
- **FR-004**: When a creation step fails: if no ERP row can have been written, the system MUST report a definite failure and allow a retry; if any ERP row may exist (including after a timeout or ambiguous response), the system MUST mark the request `needs_reconciliation`, MUST NOT delete ERP records or retry automatically, and MUST report which records may exist. Failure to attach an optional note after a successful order is reported as a warning and is not treated as a partial record.
- **FR-005**: The system MUST prevent duplicate CO/job creation when the same request is submitted more than once, and MUST NOT replay a request in `needs_reconciliation` until a person resolves it.
- **FR-006**: The system MUST allow adding an internal or external note to a CO or job record.
- **FR-007**: The system MUST allow retrieving the notes attached to a given CO or job, including type and creation date, optionally limited to notes created since a given date.
- **FR-008**: The system MUST return a clear, structured result for every request indicating success or failure, with ERP error text on failure, and MUST provide a status request that returns the upload state for a given job ID.
- **FR-015**: The system MUST NOT lose an accepted order: once a job ID has been returned, the confirmed record is retained even if the ERP or database is unavailable, and the upload is carried out later by the background worker.
- **FR-009**: The system MUST keep the existing staged connectivity check and credential-free dry run working unchanged.
- **FR-010**: The system MUST keep ERP credentials out of logs and responses, and read them from protected secret storage.
- **FR-011**: The system MUST log each request (type, record identifiers, outcome, duration) so operators can trace what was written to the ERP.
- **FR-012**: The system MUST target a configurable SyteLine company/configuration so test and production can be separated.
- **FR-016**: The system MUST NOT perform any write to the live ERP unless live writes are explicitly enabled by configuration, and the dry run MUST NOT open a SQL connection or write anything. Live writes are enabled only for an explicitly approved test.
- **FR-017**: The system MUST accept only requests authenticated with an approved AWS identity, MUST require `uploaded_by` on every order, MUST accept only operator-confirmed records, and MUST record `uploaded_by` and the caller identity with the upload and in the request log.
- **FR-013**: The system MUST create one job per customer-order line, using that line's item and quantity, and link each job to its line. The response MUST list every job created.
- **FR-014**: The system MUST resolve the customer from caller-supplied identifying details against SyteLine's customer address table, MUST take the customer number and commercial terms only from that match (never from the caller or OCR), and MUST reject the request, writing nothing, when there is no match or more than one match. The matching rules follow the existing PO OCR resolver.

### Key Entities

- **Customer Order (CO)**: Sales order record in SyteLine, identified by a CO number, with customer and line items.
- **Customer Match**: The result of resolving caller-supplied customer details to exactly one SyteLine customer (number and terms come from SyteLine).
- **Job**: Production job for one CO line, identified by a job number.
- **Note**: Text attached to a CO or job; internal or external; has creation date.
- **Integration Request**: A submitted unit of work (create order/job, add note, get notes, health check) with a unique reference used to detect duplicates. An order upload has a job ID and a state: `accepted` → `running` → `succeeded` | `failed` | `needs_reconciliation`.
- **Integration Result**: Outcome of a request: success/failure, created identifiers, validation or ERP error details.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A valid order request receives a job ID within 5 seconds, and its CO and jobs are visible in SyteLine with identifiers available from the status request within 60 seconds in 95% of requests.
- **SC-002**: In 100% of failures where an ERP row may exist, the request ends in `needs_reconciliation`, the possibly affected records are reported, and no automatic delete or retry occurs.
- **SC-003**: Submitting the same request 5 times creates exactly one CO/job.
- **SC-004**: 20 simultaneous requests produce 20 distinct CO/job numbers with zero collisions.
- **SC-005**: Notes added to a record are retrievable within 5 seconds and match the submitted text exactly.
- **SC-006**: Every failed request names the failing step and includes the ERP's message, so an operator can act without reading ERP logs.
- **SC-007**: The connectivity health check produces the same stage results as before the change.

## Assumptions

- Upstream callers (such as the PO OCR project) are internal applications that authenticate with an approved AWS identity and reach the system through a private API; individual operators do not sign in to this system, but the confirming operator is passed as `uploaded_by`.
- The ERP's supplied SOAP service, its existing numbering and note-handling routines (see `sql/`), and the CO/job creation field mapping in `docs/COJobCreationIDOs.xlsx` define what data is required.
- There is no non-production SyteLine environment. Live ERP writes require explicit approval from the ERP owner for each test, together with a reconciliation and cleanup plan.
- Order changes, cancellations, and deletes are out of scope for this version; only create and notes.
- Order volume is low (tens per hour), so batch/bulk processing is out of scope.
- The Lambda can reach the SyteLine SQL database in addition to the SOAP service (host and listener port to be confirmed with Chromalloy IT; not assumed to be 1433).
- The ERP remains reachable over the existing network path (private network or gateway) used by the current function.
