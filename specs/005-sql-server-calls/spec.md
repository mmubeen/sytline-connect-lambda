# Feature Specification: SQL Server Calls for Numbers and Customer Lookup

**Feature Branch**: `005-sql-server-calls`

**Created**: 2026-10-22

**Status**: Draft

**Input**: User description: "use sql server calls where needed as per the PO OCR demo program (COMPLETE_OCR)"

## Background

The PO OCR demo program talks to SyteLine in two ways. Records (customer order header, lines, jobs, teardown date) are saved through the SyteLine web service. A small number of things are done with **direct calls to the SyteLine database (SQL Server)** instead, because the web service does not offer them reliably:

1. **Reserving the next customer-order number and job (work-order) numbers**, by running SyteLine's own numbering routines (`NextCoSp`, `NextWOSp`) and committing, so the numbers are reserved to the caller.
2. **Finding the customer** from the name and shipping address on a PO, by searching the customer address data and scoring the candidates (a confident single match is accepted; low-confidence or ambiguous matches are refused).
3. **Reading the customer's default terms code and ship-via code** from the customer's most recent order, to fill the new order header.

The integration built so far (features 001, 003 and 004) deliberately avoided any database connection and guessed the web-service names for these three things, which are unconfirmed. This feature brings those three calls back, as the demo does them, and **only those three**, and only **for an operation the web service has no endpoint for**: where an endpoint is configured for an operation, the web service is used as before. Everything that creates or changes a record in SyteLine continues to go through the web service.

## Clarifications

### Session 2026-10-22

- Q: Which calls use the database? → A: Exactly the three the demo uses: reserving CO and job numbers, finding the customer, and reading the customer's default terms and ship-via codes. No other read or write goes to the database.
- Q: Does the database ever create, change or delete records? → A: No. The only change it makes is the one SyteLine's own numbering routines make when they reserve a number (committed, exactly as the demo does). Header, lines, jobs, teardown dates and notes are still saved through the web service.
- Q: When the database connection is configured, does it replace the web-service numbering and customer lookup of the earlier features? → A: No. The web service is the first choice: the database is used only for an operation that has no web-service endpoint. Whether an operation has one is decided in advance, per operation, by a setting (an endpoint name is configured, or it is empty), never by trying the web service and reacting to a failure; once a call has been sent, the route is never switched.
- Q: Must the database connection be encrypted, and must its certificate be verified? → A: Yes to both by default. Encryption is required and the server's certificate is verified; verification can be switched off only by an explicit setting (for example a self-signed test server), which is logged as a warning on every use. The demo's unencrypted, unverified connection is not carried over.
- Q: Where are the database login and password stored? → A: In the existing ERP credentials secret, alongside the web-service credentials (no separate secret). Every function that can read that secret can therefore read the database settings; this is accepted, and the database login is kept to the minimum rights these calls need.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Reserve CO and job numbers from SyteLine's numbering routines (Priority: P1)

When a PO is reserved (the job-number workflow) or uploaded in one step, and the numbering operation has no web-service endpoint configured, the CO number and one job number per line are obtained by running SyteLine's own numbering routines through a database connection, and committed so the numbers belong to the caller. A number obtained is never reissued, even if a later step fails (it leaves a gap, as it does for SyteLine's own users).

**Why this priority**: the numbers are the key the whole order depends on, and the web-service route to them is unconfirmed; the demo's database route is the known-good one.

**Independent Test**: Reserve numbers for a 3-line PO against a simulated database; one CO number and three job numbers come back in order, nothing else is written, and 20 concurrent reservations never share a number.

**Acceptance Scenarios**:

1. **Given** the numbering operation has no web-service endpoint configured, the database connection is configured and live writes are enabled, **When** numbers are reserved for a PO of N lines, **Then** one CO number and N job numbers are returned, in order, each committed before it is returned.
2. **Given** a numbering routine reports a problem or returns no number, **When** a reservation is attempted, **Then** the attempt is rolled back, nothing is reported as reserved, and the failure names the routine and the database's message.
3. **Given** live writes are not enabled, **When** a reservation is attempted, **Then** the database is not asked for any number and the usual "live writes disabled" result is returned.
4. **Given** the connection drops after a numbering routine was started but before the result was received, **When** the outcome is unknown, **Then** it is reported as uncertain (a number may have been consumed), never as a clean failure, and is never retried automatically.

---

### User Story 2 - Find the customer from the PO's name and address (Priority: P1)

The customer on a PO is found, when the lookup has no web-service endpoint configured, by searching SyteLine's customer address data through the database with the name and shipping address from the document. Candidates are scored on name and address; the customer is accepted only when exactly one candidate is a confident match. A weak or tied match is refused, naming the best candidates and their scores, so a PO is never booked against a guessed customer.

**Why this priority**: booking an order against the wrong customer is a data-integrity problem; this is the check that prevents it, and the demo's matching rules are the reference.

**Independent Test**: Run a reference set of customer names and addresses (clear matches, near-duplicates, unknown names, vague names) through the lookup; each gets the same decision as the demo (match, ambiguous, low confidence or not found).

**Acceptance Scenarios**:

1. **Given** a name and address that clearly identify one customer and ship-to address, **When** the lookup runs, **Then** it returns the customer number, ship-to address number and the matched name, with status `match`.
2. **Given** two candidates whose scores are within the ambiguity margin, **When** the lookup runs, **Then** it returns `ambiguous` with both candidates and scores, and the PO is refused.
3. **Given** the best candidate's score is below the confidence threshold, **When** the lookup runs, **Then** it returns `low_confidence` with the best candidate and its score, and the PO is refused.
4. **Given** no candidate shares any significant part of the name, **When** the lookup runs, **Then** it returns not found.
5. **Given** a name or address containing quotes or other special characters, **When** the lookup runs, **Then** it is treated purely as text and cannot alter the query.

---

### User Story 3 - Fill the order header's terms and ship-via from the customer's last order (Priority: P2)

When the order header is saved, and reading the defaults has no web-service endpoint configured, its terms code and ship-via code are read through the database from the customer's most recent order: first an order for the same customer and ship-to address, otherwise any order for the customer. A customer with no previous order gets a header without them.

**Why this priority**: it matches what the operator would get today and avoids a hand-edit after every upload, but the order can still be created without it.

**Independent Test**: For a customer with orders at two ship-to addresses, a customer with orders only at other addresses, and a customer with no orders, check the codes chosen.

**Acceptance Scenarios**:

1. **Given** the customer has an order for the same ship-to address, **When** defaults are read, **Then** that most recent order's terms and ship-via codes are used.
2. **Given** only orders for other ship-to addresses exist, **When** defaults are read, **Then** the customer's most recent order's codes are used.
3. **Given** the customer has no orders, **When** defaults are read, **Then** the header is saved without terms and ship-via and the result says so.
4. **Given** the defaults cannot be read, **When** the order is saved, **Then** the save is not blocked: the header goes without them and a warning says why.

---

### User Story 4 - Know the database path works before relying on it (Priority: P3)

The connectivity check reports whether the database can be reached and the login works, using only a harmless read, so a deployer can find a missing route, a closed port or a wrong login before the first PO. A failure is described in plain language.

**Why this priority**: the database sits on the plant network and is the most likely thing to be unreachable; seeing that early saves a failed upload.

**Independent Test**: Run the check against a reachable database, an unreachable one and one with a bad login; each result names the stage that failed.

**Acceptance Scenarios**:

1. **Given** a reachable database and valid login, **When** the check runs, **Then** it reports the database stage as passed and writes nothing.
2. **Given** the database cannot be reached, or the login is refused, **When** the check runs, **Then** it reports which stage failed (name lookup, connection, login) without exposing the credentials.

---

### Edge Cases

- Database unreachable or login refused (for an operation that uses the database): the request fails clearly and nothing is reserved or written; nothing falls back to another route.
- An operation whose web-service endpoint is configured but not working fails clearly; it is never retried through the database.
- A numbering routine commits but the response is lost: reported as uncertain; the number is treated as consumed.
- Two reservations at the same moment: the database's routines serialize them; no number is issued twice.
- A very common customer name matches hundreds of rows: the candidate list is capped and the decision is still made on scores, never on an arbitrary first row.
- A customer with several ship-to addresses and a PO address that matches none of them well: refused as low confidence or ambiguous, not defaulted to the bill-to.
- Names with punctuation, accents, "Inc"/"LLC"-style suffixes or abbreviations ("Street"/"St"): normalised before scoring, as in the demo.
- The database is in maintenance or read-only: reads work, reservations fail clearly (no number reserved).
- Credentials rotated: the next request uses the new ones; they never appear in logs or responses.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: When the numbering operation has no web-service endpoint configured, the system MUST obtain the customer-order number and the job numbers by running SyteLine's own numbering routines through a database connection and committing each reserved number before returning it, one CO number and one job number per line.
- **FR-002**: A numbering call that reports a problem, returns no row or an unreadable number MUST be rolled back, MUST NOT be reported as reserved, and MUST surface the routine's name and message.
- **FR-003**: A reservation whose outcome is unknown (connection lost after the routine started) MUST be reported as uncertain and MUST NOT be retried automatically; the number MUST be treated as consumed.
- **FR-004**: The system MUST NOT run any numbering routine unless live ERP writes are enabled, because reserving a number is itself a change in SyteLine.
- **FR-005**: When the customer lookup has no web-service endpoint configured, the system MUST find the customer by searching the customer address data with the name and shipping address from the document, normalising names and addresses and scoring candidates as the demo does (same confidence threshold and ambiguity margin, configurable).
- **FR-006**: The system MUST accept a customer only when exactly one candidate is a confident match, and MUST otherwise refuse with the status (`ambiguous`, `low_confidence` or not found), the best candidates and their scores, writing and reserving nothing.
- **FR-007**: When reading the defaults has no web-service endpoint configured, the system MUST read the customer's default terms code and ship-via code from the customer's most recent order (same customer and ship-to first, then any order of the customer) and use them in the order header; a customer without orders, or an unreadable default, MUST NOT block the save and MUST be reported.
- **FR-008**: Every database query MUST treat document and caller text purely as data (parameterised); no text from a document or request may change the shape of a query.
- **FR-009**: Apart from the change SyteLine's numbering routines make when reserving a number, the database connection MUST NOT insert, update or delete anything; every record (order header, lines, jobs, teardown dates, notes) MUST continue to be saved through the web service.
- **FR-010**: Database credentials (server, database, login, password) MUST come from protected secret storage (the existing ERP credentials secret), MUST never appear in logs or responses, and the login SHOULD have only the rights these calls need. The connection MUST be encrypted and the server's certificate MUST be verified; verification MAY be switched off only by an explicit setting, and every connection made with it off MUST be logged with a warning (encryption itself cannot be switched off).
- **FR-011**: The connectivity check MUST report which route (web service or database) each of the three operations uses and, when any uses the database, the database stages (name lookup, connection, login) separately, using only a harmless read, and MUST NOT expose credentials.
- **FR-012**: For each of the three operations (reserving numbers, finding the customer, reading the customer's defaults) the system MUST use the web service when an endpoint name is configured for it and MUST use the database only when none is configured. The choice MUST be made from that setting before anything is sent; the system MUST NOT try one route and switch to the other after a failure, a timeout or an unknown outcome, so one PO's numbers never come from two routes. A deployer MUST be able to see which route each operation uses (in the connectivity check).
- **FR-013**: Database work MUST stay within the existing time limits: a reservation for up to 25 lines MUST still finish within the reserve step's time budget, and the connection MUST be closed when the request ends.
- **FR-014**: The system MUST log each database-backed step (type, identifiers, outcome, duration, number of candidates) without credentials or customer personal data beyond what the existing logs already carry.

### Key Entities

- **Reserved Number**: A CO or job number obtained from SyteLine's numbering routines and committed to the caller; never reissued, even if unused.
- **Customer Candidate**: A row of customer address data considered for a PO: customer number, ship-to address number, name, address lines, city, state, postal code, country, and a score.
- **Customer Match**: The decision for one PO: status (`match`, `ambiguous`, `low_confidence`, not found), the chosen customer and ship-to when `match`, otherwise the best candidates with scores.
- **Customer Defaults**: The terms code and ship-via code of the customer's most recent order, with which order they came from (same ship-to or any).
- **Database Connection Settings**: Server, database, login and password, held in protected storage and used only for the operations that have no web-service endpoint configured.
- **Operation Route**: For each of the three operations, the setting that names its web-service endpoint, or is empty to mean the database.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In concurrent testing with at least 20 simultaneous reservations against the numbering routines, 100% of the CO and job numbers issued are distinct.
- **SC-002**: A reservation for a 25-line PO (one CO number, 25 job numbers) completes in under 15 seconds, leaving time inside the reserve step's limit for the customer lookup and the document read.
- **SC-003**: On a reference set of at least 30 customer names and addresses (clear matches, near-duplicates, unknown and vague names), the lookup gives the same decision as the demo program for 100% of them.
- **SC-004**: In 100% of tests where the match is ambiguous, low confidence or not found, no number is reserved and no record is written.
- **SC-005**: An audit of every database statement run in the test suite finds 0 statements that insert, update or delete a row directly; the only changes come from the numbering routines.
- **SC-006**: In every failure-injection test (connection dropped, login refused, routine error), no number is reported as reserved unless it was committed, and an unknown outcome is always reported as uncertain, never as a clean failure.
- **SC-007**: No test output, log line or response contains the database password or login.
- **SC-010**: In 100% of tests a database connection is made encrypted with certificate verification on unless the explicit setting is given, in which case a warning is logged; no test can make an unencrypted connection.
- **SC-008**: A deployer can tell from one connectivity check which route each of the three operations uses and, for any that use the database, whether the database name resolves, accepts a connection and accepts the login, without making any change.
- **SC-009**: In 100% of failure-injection tests (web service refused, timed out or unknown outcome), the system never starts the same operation on the other route.

## Assumptions

- The demo's behaviour is the reference: the same two numbering routines, the same customer address search and scoring (threshold 70, ambiguity margin 3, candidate cap 1000 as defaults), and the same most-recent-order rule for defaults. Values can be tuned by configuration.
- The earlier features' rule "no database connection of any kind" is relaxed for exactly these three calls; the safeguard tests that enforce it will be changed to allow these calls and to forbid any other use of the database.
- The functions already run in the plant-routed network; the database server is reachable from it on its normal port once the network team opens the route and the security group allows it. This feature does not create network rules.
- The database server presents a certificate the functions can verify (issued by a trusted authority, or its authority added to the functions' trust); a self-signed test server needs the explicit setting.
- A database login with rights to run the two numbering routines and read the customer address and order tables exists or will be created; a login with only those rights is preferred.
- The database server, database name, login and password are added to the existing ERP credentials secret; how the database driver is packaged for the functions is a planning decision, not part of this specification.
- Which route an operation uses is a per-operation setting (an endpoint name, or empty for the database); nothing is detected at run time and no failure ever switches the route, so one PO's numbers never come from two sources.
- The matched customer's number and ship-to address number are taken only from the database result, never from the caller or the document, as before.
- The stand-alone (no-VPC) version is out of scope: it cannot reach the plant database.
