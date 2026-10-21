# Feature Specification: SyteLine ERP Integration

**Feature Branch**: `001-syteline-erp-integration`

**Created**: 2026-10-20

**Status**: Draft

**Input**: User description: "update lambda implementation to integrate with ERP sytline"

## Background

Today the Lambda only proves that the SyteLine ERP is reachable and that credentials work (staged DNS / network / service-description / login checks). It does not read or write any ERP business data. This feature turns it into a working integration: upstream systems (e.g. the PO OCR project) can ask it to create customer orders (CO) and jobs in SyteLine, attach notes to those records, and read notes back, while the existing connectivity check is preserved as a health check.

## Clarifications

### Session 2026-10-20

- Q: May the diagnostics contact anything other than the ERP, to tell "no outbound network at all" apart from "only the ERP is unreachable"? → A: Only the ERP host and the function's own regional AWS endpoint (the secret store): a name lookup and a connection test to each, nothing else, no data sent and no credentials.
- Q: When should the extra connectivity diagnostics of the health check run? → A: Only when asked: `"diagnose": true` in the event. A check without the flag, passing or failing, gives no extra detail and its stage list and result are unchanged. The diagnostics are reported in a separate `diagnostics` section, never inside `stages`.
- Q: Which AWS region should the stack deploy to? → A: us-gov-east-1 (GovCloud). It is a single value in the deploy configuration; partition-specific ARNs use `aws-us-gov`.
- Q: Should the SAM setup define one deployment, or separate staging and production deployments? → A: One deployment: a single stack with one configuration (`default`). Live writes stay off in the configuration, and turning them on is a deliberate, approved change (FR-016).
- Q: Through which channel may the system access SyteLine's database? → A: Only through the SyteLine web service (IDO SOAP). The system makes no direct SQL connection and holds no database credentials. (Source: user direction; supersedes the SQL steps in instruction sheet section 4 and the SQL-based customer lookup, number allocation and notes in the earlier answers.)
- Q: How should CO and job numbers be obtained through the web service? → A: By calling SyteLine's `NextCoSp` and `NextWOSp` routines as methods through the web service, then inserting the rows with the allocated numbers. Allocation is a separate step before the insert and counts as a write for the live-write gate. (Whether these are callable as web-service methods is confirmed by the read-only discovery task.)
- Q: How should the customer be looked up through the web service? → A: With a read-only query on SyteLine's customer address collection (the data behind `custaddr`), restricted to the bill-to row and filtered by the supplied name and address. If the web service exposes no such collection, the read-only discovery task reports it and this is revisited. The exact collection name is confirmed by that task.
- Q: How should notes be written and read through the web service? → A: Create notes by calling `CreateSpecificNoteSp` as a web-service method (it creates and attaches the note). Read notes by querying the `SpecificNotes` and `ObjectNotes` collections, linked through the record's row pointer. Note creation is a write and stays behind the live-write gate.
- Q: When a create-order attempt fails or times out after SyteLine may already have accepted some rows, what should the system do? → A: If the failure is a definite rejection before any row was written, a retry is allowed. If any row may exist, mark the request `needs_reconciliation`, make no automatic delete or retry, and report which records may exist. (Source: `docs/Instruction Sheet AWS GovCloud API Gateway to On-Premises Sy.docx`, section 5.)
- Q: How should the system identify the customer when an order comes in? → A: The caller sends customer identifying details (e.g. bill-to name and address), never a customer number. The system resolves the customer in SyteLine's customer address data through the web service and refuses the request on no match or an ambiguous match. (Source: instruction sheet, section 4.)
- Q: Should creating an order return a upload ID right away, with the upload running in the background and a status call, or should the caller wait for the final result? → A: Asynchronous. `create_order` validates, durably accepts the confirmed order and returns a upload ID immediately; a background worker performs the upload; a status request returns the state (`accepted`, `running`, `succeeded`, `failed`, `needs_reconciliation`) plus CO and job numbers when done. (Source: instruction sheet, sections 3–4.)
- Q: Until there is an explicit go-ahead, should anything be written to the live SyteLine ERP, including test orders and field-name discovery? → A: No. There is no test ERP; no live write happens without explicit approval from the ERP owner for that specific test. Until then validation uses mocked and dry-run tests plus read-only network and web-service checks. Any approved live test has a documented reconciliation and cleanup plan. (Source: instruction sheet, section 6.)
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

- **FR-001**: The system MUST accept requests to create a customer order and associated jobs in SyteLine. It MUST validate the request, durably record it, and return a upload ID immediately; the CO and job identifiers are returned through the status request once the upload finishes.
- **FR-002**: The system MUST validate request data before contacting the ERP and report all validation problems together.
- **FR-003**: The system MUST obtain CO and job numbers through the web service by calling SyteLine's own numbering routines (CO and job number allocation) as methods, so numbers never collide with those issued by other SyteLine users.
- **FR-004**: When a creation step fails: if no ERP row can have been written, the system MUST report a definite failure and allow a retry; if any ERP row may exist (including after a timeout or ambiguous response), the system MUST mark the request `needs_reconciliation`, MUST NOT delete ERP records or retry automatically, and MUST report which records may exist. Failure to attach an optional note after a successful order is reported as a warning and is not treated as a partial record.
- **FR-005**: The system MUST prevent duplicate CO/job creation when the same request is submitted more than once, and MUST NOT replay a request in `needs_reconciliation` until a person resolves it.
- **FR-006**: The system MUST allow adding an internal or external note to a CO or job record.
- **FR-007**: The system MUST allow retrieving the notes attached to a given CO or job, including type and creation date, optionally limited to notes created since a given date.
- **FR-008**: The system MUST return a clear, structured result for every request indicating success or failure, with ERP error text on failure, and MUST provide a status request that returns the upload state for a given upload ID.
- **FR-015**: The system MUST NOT lose an accepted order: once a upload ID has been returned, the confirmed record is retained even if the ERP or database is unavailable, and the upload is carried out later by the background worker.
- **FR-009**: The system MUST keep the existing staged connectivity check and credential-free dry run working unchanged.
- **FR-010**: The system MUST keep ERP credentials out of logs and responses, and read them from protected secret storage.
- **FR-011**: The system MUST log each request (type, record identifiers, outcome, duration) so operators can trace what was written to the ERP.
- **FR-012**: The system MUST target a configurable SyteLine company/configuration so test and production can be separated.
- **FR-016**: The system MUST NOT perform any write to the live ERP unless live writes are explicitly enabled by configuration, and the dry run MUST NOT send credentials, log in or write anything. Live writes are enabled only for an explicitly approved test.
- **FR-019**: The health check MUST offer an opt-in `diagnose` option that adds a separate `diagnostics` section without changing `stages` or the overall result. The diagnostics MUST (a) report the resolver settings the function sees, (b) look up the ERP host name, retrying a few times on transient errors and reporting every attempt, (c) test a connection to each resolved ERP address and port, (d) do the same lookup and connection test for the function's own regional AWS endpoint (secret store), and (e) state in plain words what the combination of results most likely means. They MUST contact only the ERP host and that AWS endpoint and, for the lookup comparison, send a DNS query for those two names to the function's own nameserver(s) (added 2026-10-21: a direct nameserver answer separates a resolver refusal from a failure inside the function's own lookup); they send no data and no credentials, read no secret, and be skipped (with the reason) when the HTTPS rule refuses the ERP address.
- **FR-018**: The system MUST read and write SyteLine data only through the SyteLine web service. It MUST NOT open a direct database (SQL) connection, and no database credentials are configured.
- **FR-017**: The system MUST accept only requests authenticated with an approved AWS identity, MUST require `uploaded_by` on every order, MUST accept only operator-confirmed records, and MUST record `uploaded_by` and the caller identity with the upload and in the request log.
- **FR-013**: The system MUST create one job per customer-order line, using that line's item and quantity, and link each job to its line. The response MUST list every job created.
- **FR-014**: The system MUST resolve the customer from caller-supplied identifying details against SyteLine's customer address data (read through the web service), MUST take the customer number and commercial terms only from that match (never from the caller or OCR), and MUST reject the request, writing nothing, when there is no match or more than one match. The matching rules follow the existing PO OCR resolver.

### Key Entities

- **Customer Order (CO)**: Sales order record in SyteLine, identified by a CO number, with customer and line items.
- **Customer Match**: The result of resolving caller-supplied customer details to exactly one SyteLine customer (number and terms come from SyteLine).
- **Job**: Production job for one CO line, identified by a job number.
- **Note**: Text attached to a CO or job; internal or external; has creation date.
- **Upload ID**: The tracking identifier returned when an order is accepted, used with the status request. It is not a SyteLine job number; the term "job" in this spec always means a SyteLine production job.
- **Integration Request**: A submitted unit of work (create order/job, add note, get notes, health check) with a unique reference used to detect duplicates. An order upload has a upload ID and a state: `accepted` → `running` → `succeeded` | `failed` | `needs_reconciliation`.
- **Integration Result**: Outcome of a request: success/failure, created identifiers, validation or ERP error details.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A valid order request receives a upload ID within 5 seconds, and its CO and jobs are visible in SyteLine with identifiers available from the status request within 60 seconds in 95% of requests.
- **SC-002**: In 100% of failures where an ERP row may exist, the request ends in `needs_reconciliation`, the possibly affected records are reported, and no automatic delete or retry occurs.
- **SC-003**: Submitting the same request 5 times creates exactly one CO/job.
- **SC-004**: 20 simultaneous requests produce 20 distinct CO/job numbers with zero collisions.
- **SC-005**: Notes added to a record are retrievable within 5 seconds and match the submitted text exactly.
- **SC-006**: Every failed request names the failing step and includes the ERP's message, so an operator can act without reading ERP logs.
- **SC-007**: The connectivity health check produces the same stage results as before the change.
- **SC-008**: From one health invocation with `diagnose` set, an operator can tell whether name resolution works at all, whether the ERP name resolves, and whether a connection to the ERP and to the AWS endpoint succeeds, without reading logs or running anything else.

## Assumptions

- Upstream callers (such as the PO OCR project) are internal applications that authenticate with an approved AWS identity and reach the system through a private API; individual operators do not sign in to this system, but the confirming operator is passed as `uploaded_by`.
- The ERP's supplied SOAP service, its existing numbering and note-handling routines (see `sql/`, used as a reference for behavior; they are reached only through the web service), and the CO/job creation field mapping in `docs/COJobCreationIDOs.xlsx` define what data is required.
- The service deploys to AWS GovCloud region us-gov-east-1.
- There is no non-production SyteLine environment. Live ERP writes require explicit approval from the ERP owner for each test, together with a reconciliation and cleanup plan.
- Order changes, cancellations, and deletes are out of scope for this version; only create and notes.
- Order volume is low (tens per hour), so batch/bulk processing is out of scope.
- The Lambda reaches SyteLine only through the SOAP web service (HTTP, port 80 as documented). No SQL host, port or credentials are needed.
- The ERP remains reachable over the existing network path (private network or gateway) used by the current function.
