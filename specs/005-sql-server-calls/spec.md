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
- Q: Must the database connection be encrypted, and must its certificate be verified? → A: By default yes to both: the connection is encrypted and the server's certificate is verified against a configured certificate authority. (Refined below: the deployer can explicitly allow an unencrypted connection, so no certificate authority file is needed.)
- Q: When no CA certificate is configured for the SyteLine database, should the connection still be encrypted (certificate not checked), be allowed to run unencrypted, or still be refused? → A: An explicit setting allows an **unencrypted** connection, as the demo's connection does (`Encrypt=no`), and with it no CA certificate is needed. Every connection made that way logs a warning, and the connectivity check reports that the connection is unencrypted. Without the setting the default holds: encrypted, certificate verified, CA certificate required. The deployer accepts that the database login and password then cross the plant network unprotected (the same kind of explicit, warned override the web-service address has for plain HTTP).
- Q: Where are the database login and password stored? → A: In the existing ERP credentials secret, alongside the web-service credentials (no separate secret). Every function that can read that secret can therefore read the database settings; this is accepted, and the database login is kept to the minimum rights these calls need.
- Q: What is wanted beyond the current connectivity check (confirm it, add a `GET /health` API route with the database check, or run the database stages whatever the routes are)? → A: Confirm the current behaviour; no change. The connectivity check is the direct-invoke health action. In a full run it already includes the database stages (name lookup, connection, login) whenever at least one operation uses the database, and it always reports each operation's route. The credential-free dry run reports routes only (the database server name is read from the secret), and no HTTP route for the health check is added.
- Q: What should the stand-alone Lambda do with the SQL Server settings? → A: Its connectivity check includes the database test, and nothing else. When the stand-alone version is attached to an existing VPC it reports the database stages (name lookup, connection, login), `routes` and the unencrypted warning, with the same rules as the plant version (encrypted and verified by default, the explicit warned unencrypted option, credentials only from the secret). It gains the database keys in its secret, the encryption and timeout settings, and no number reservation, customer search, defaults or order save (it has none of those actions). Without a VPC attachment it does not run the database stages.
- Q: Should the stand-alone health check run the database check automatically whenever the function is attached to a VPC, instead of requiring `DATABASE_CHECK=true`? → A: Yes, automatically whenever it is attached, even if the secret holds no database keys (a missing key then shows as a failing `db_login` stage that names the keys). The setting becomes three-valued: `auto` (the new default: run when attached, skip with a note when not), `true` (always run; refused at deployment without the VPC attachment) and `false` (never run). This supersedes the earlier default of off, so an attached stand-alone deployment now shows the database stages without any extra setting.
- Q: How far should the refactor go to keep the plant mode and the stand-alone mode in sync? → A: Parity tests only. The test suite fails whenever a setting that both modes share differs between the two templates, `deploy.sh` and the env example files in name, default, allowed values or wiring to the function. No structural change: the two templates, the two deploy branches and the two example files stay as they are, and no stack is affected.
- Q: How should the database login and password reach the deployment from the env files? → A: The deploy script reads them from the settings file (git-ignored) and merges them into the existing credentials secret before deploying: it asks first (a flag skips the question), keeps the secret's other keys, and never passes them to CloudFormation, a command line or the output. The functions still read them only from the secret.
- Q: Should both stacks read one shared settings file instead of two? → A: Yes. One file, `deploy.env`, and one `deploy.env.example` serve both `./deploy.sh` and `./deploy.sh --standalone`. Settings both modes share (ERP address, user, secret, database login, SQL options, live-writes gate, FIPS) are written once; the plant-only block (`WORKER_*`, network creation, API endpoint, IDP, route and tuning settings) and the stand-alone-only block (`ATTACH_*`, `ALLOW_PLAIN_HTTP`, `VERIFY_TLS`) are clearly marked, and each mode ignores the other's block. `deploy.standalone.env` and its example are removed.
- Q: What should change about the two deploy messages (the network reminder and the secret-merge notice)? → A: Wording only. The reminder reads "Check: the subnets need a route to the ERP and a way to reach Secrets Manager (VPC endpoint or NAT). The stack does not create either. See docs/STANDALONE.md." and the merge notice reads "Will add or update these keys in the credentials secret: <keys>. All other keys stay as they are, and values are never printed." Behaviour (when each prints, the confirmation question, `--yes`) is unchanged.

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
- **FR-010**: Database credentials (server, database, login, password) MUST come from protected secret storage (the existing ERP credentials secret), MUST never appear in logs or responses, and the login SHOULD have only the rights these calls need. By default the connection MUST be encrypted and the server's certificate MUST be verified against a configured certificate authority (which is then required). Only an explicit setting MAY allow an unencrypted connection, in which case no certificate authority is needed, every such connection MUST be logged with a warning, and the connectivity check MUST say the connection is unencrypted; no other setting or failure may ever cause an unencrypted connection (for example, a missing certificate authority MUST NOT silently fall back to unencrypted).
- **FR-011**: The connectivity check MUST report which route (web service or database) each of the three operations uses and, when any uses the database, the database stages (name lookup, connection, login) separately, using only a harmless read, MUST say whether the connection is encrypted (and warn when it is not), and MUST NOT expose credentials. The stand-alone version's connectivity check MUST include the same database stages and encryption reporting automatically whenever it is attached to a VPC (unless switched off), MUST NOT run them when it is not attached, and MUST name any missing database key in the failing stage.
- **FR-012**: For each of the three operations (reserving numbers, finding the customer, reading the customer's defaults) the system MUST use the web service when an endpoint name is configured for it and MUST use the database only when none is configured. The choice MUST be made from that setting before anything is sent; the system MUST NOT try one route and switch to the other after a failure, a timeout or an unknown outcome, so one PO's numbers never come from two routes. A deployer MUST be able to see which route each operation uses (in the connectivity check).
- **FR-013**: Database work MUST stay within the existing time limits: a reservation for up to 25 lines MUST still finish within the reserve step's time budget, and the connection MUST be closed when the request ends.
- **FR-014**: The system MUST log each database-backed step (type, identifiers, outcome, duration, number of candidates) without credentials or customer personal data beyond what the existing logs already carry.
- **FR-016**: The deployment script MUST be able to merge the database settings given in the settings file into the existing credentials secret, keeping its other keys, only after confirmation, without ever placing the values in stack parameters, command lines or output, and MUST stop before building or deploying if the secret cannot be read, written or merged.
- **FR-015**: A setting that exists in both the plant and the stand-alone deployment (same stack parameter or environment variable) MUST have the same name, default, allowed values and function wiring in both templates, the same default and validation in `deploy.sh`, and an entry in the single deploy example file, and the test suite MUST fail when any of these drift apart.
- **FR-017**: Both deployment modes MUST read the same settings file (`deploy.env`, with one `deploy.env.example`); shared settings appear once, mode-specific settings sit in marked blocks, a mode ignores the other mode's block, and no separate stand-alone settings file or example remains.
- **FR-018**: The attached stand-alone deployment MUST print, before building, a reminder that names both needs (a route to the ERP and a way to reach Secrets Manager through a VPC endpoint or a NAT route), says the stack creates neither and points to `docs/STANDALONE.md`; and the secret-merge notice MUST say the listed keys are added or updated, that all other keys stay as they are and that values are never printed. Neither message prints a value.

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
- **SC-010**: In 100% of tests a database connection is encrypted with certificate verification unless the explicit unencrypted setting is given; with that setting every connection logs a warning and the connectivity check reports it; in no test does a missing certificate authority, a failed handshake or any other error lead to an unencrypted connection without that setting.
- **SC-011**: Deliberately changing the default or allowed values of any shared setting in only one of the two templates (or in only one deploy branch or example file) makes at least one automated test fail.
- **SC-008**: A deployer can tell from one connectivity check which route each of the three operations uses and, for any that use the database, whether the database name resolves, accepts a connection and accepts the login, without making any change.
- **SC-009**: In 100% of failure-injection tests (web service refused, timed out or unknown outcome), the system never starts the same operation on the other route.

## Assumptions

- The demo's behaviour is the reference: the same two numbering routines, the same customer address search and scoring (threshold 70, ambiguity margin 3, candidate cap 1000 as defaults), and the same most-recent-order rule for defaults. Values can be tuned by configuration.
- The earlier features' rule "no database connection of any kind" is relaxed for exactly these three calls; the safeguard tests that enforce it will be changed to allow these calls and to forbid any other use of the database.
- The functions already run in the plant-routed network; the database server is reachable from it on its normal port once the network team opens the route and the security group allows it. This feature does not create network rules.
- The plant database server may not present a certificate the functions can verify (the demo connects without encryption). Reaching it then needs the explicit unencrypted setting; once the server presents a certificate from an authority the deployer can supply, the default (encrypted and verified) can be used instead.
- A database login with rights to run the two numbering routines and read the customer address and order tables exists or will be created; a login with only those rights is preferred.
- The database server, database name, login and password are added to the existing ERP credentials secret; how the database driver is packaged for the functions is a planning decision, not part of this specification.
- The connectivity check is the existing direct-invoke health action; it is not exposed as an API route in this feature.
- Which route an operation uses is a per-operation setting (an endpoint name, or empty for the database); nothing is detected at run time and no failure ever switches the route, so one PO's numbers never come from two sources.
- The matched customer's number and ship-to address number are taken only from the database result, never from the caller or the document, as before.
- The stand-alone version uses the database only for its connectivity check, which runs automatically when it is attached to an existing VPC (and can be switched off or forced on) (the database stages are skipped when it is not, since it has no route to the plant); it has no actions that reserve numbers, search customers or save orders.
