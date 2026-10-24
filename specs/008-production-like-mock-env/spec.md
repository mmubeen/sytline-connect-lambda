# Feature Specification: Production-Like Mock Environment

**Feature Branch**: `008-production-like-mock-env`

**Created**: 2026-10-24

**Status**: Draft

**Input**: User description: "create a mock set up to mimic production environment so as to run preliminary integration test as close as possible"

## Clarifications

### Session 2026-10-24

- Q: Should the SQL Server stand-in be a real engine, an emulation, or both? → A: A real SQL Server engine.
- Q: Where does mock data come from? → A: Synthetic only.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run the whole intake end to end against a mock production (Priority: P1)

A developer starts one mock environment on their own machine. It stands in for everything the deployed function talks to: the SyteLine ERP web service, the SQL Server database, the IDP document source, the credentials store, the jobs table, and the function's ability to start itself in the background. The developer sends the same processing-complete event the IDP state machine sends in production and watches the function read the document, reserve job numbers, save, and finish, with no real ERP, IDP or AWS account involved.

**Why this priority**: Today the tests replace each collaborator with an in-process stand-in, so the function's real network clients, its configuration loading, its credentials handling and its background self-start are never exercised together. Failures such as a missing module, a missing permission or a wrong address only show up after a deployment. A mock that behaves like production on the wire finds them first.

**Independent Test**: Start the mock environment, send the sample processing-complete event, and confirm the final outcome and every record the function wrote (jobs, reservations, ERP calls) match what the same event produces in production.

**Acceptance Scenarios**:

1. **Given** the mock environment is running and holds a document that passes review, **When** the sample event is sent, **Then** the function reserves job numbers, saves, and returns the same outcome fields it returns in production, with the mock ERP showing the exact calls received.
2. **Given** the mock environment is running, **When** the event is sent twice for the same document, **Then** the second run reports the document as already processed and the mock ERP shows no second write.
3. **Given** the mock environment is not running, **When** the integration tests are started, **Then** they stop with a message saying what to start, and do not report passes or failures from a half-present environment.

---

### User Story 2 - Rehearse the failures production has shown (Priority: P1)

A developer asks the mock environment to misbehave on purpose in the ways production has: the IDP read is denied, the document needs review, the ERP is unreachable or slow, the database refuses the login, the credentials store is unavailable, another save already holds the ERP write lock. They confirm the function's response, log line and hint for each one are the ones an operator would see.

**Why this priority**: Most of the recent troubleshooting was caused by environment faults (permissions, encryption keys, missing settings), not logic faults. Being able to reproduce each one on demand turns a deployment round trip into a local test.

**Independent Test**: Switch on each fault in turn, send the sample event, and confirm the response names the right cause and that nothing was written to the mock ERP.

**Acceptance Scenarios**:

1. **Given** the mock IDP is set to deny reads with an encryption-key error, **When** the event is sent, **Then** the function refuses with the IDP-unavailable outcome and, with debug on, the underlying message includes the key it was denied on.
2. **Given** the mock ERP is set to stop responding, **When** a reserve or save is attempted, **Then** the function reports the ERP as unreachable within its time budget and leaves the job in a state the five-minute sweep can retry.
3. **Given** a fault is switched off again, **When** the event is sent, **Then** the function behaves normally with no leftover state from the fault.

---

### User Story 3 - Same entry points, same settings, same limits as production (Priority: P2)

A developer runs the function through the same entry point, with the same settings names, the same time limit and memory limit, and the same permissions model as the deployed function. API Gateway-style requests, the scheduled sweep, and the self-started background actions all go through the mock the way they do in production.

**Why this priority**: A mock that is easier than production hides problems. Matching the entry points, settings and limits is what makes "as close as possible" mean something.

**Independent Test**: Run the deployment's own template through the mock's configuration check and confirm every setting the function needs is supplied under the same name; run a slow request and confirm it is cut off at the configured time limit.

**Acceptance Scenarios**:

1. **Given** the deployment template declares a setting, **When** the mock environment starts, **Then** the function sees that setting under the same name, or the mock reports it as missing.
2. **Given** a request that runs longer than the function's time limit, **When** it is run through the mock, **Then** it is stopped at the limit and reported the way production reports a timeout.
3. **Given** the function calls a service it has no permission for in the template, **When** it runs through the mock, **Then** the call is denied the way production denies it.

---

### User Story 4 - Repeatable, safe, and easy to reset (Priority: P3)

A developer or a build pipeline can reset the mock environment to a known starting state in seconds, run the integration suite unattended, and be certain nothing reached a real system.

**Why this priority**: Repeatability and safety make the suite usable by everyone; the value is real but depends on the first three stories.

**Independent Test**: Run the suite twice in a row from a reset state and confirm identical results; confirm with real network access blocked that the suite still passes.

**Acceptance Scenarios**:

1. **Given** a previous run left jobs and locks behind, **When** the environment is reset, **Then** the next run starts empty and gives the same result as the first.
2. **Given** real credentials are present on the machine, **When** the suite runs, **Then** none of them are read or sent anywhere, and live ERP writes remain off.

---

### Edge Cases

- A mock service is slow to start: the suite waits up to a stated limit, then stops with a message naming the service that was not ready.
- Two test runs start at the same time: each gets its own isolated state, or the second is refused with a clear message.
- The mock and production drift apart (a new setting, a new action, a changed ERP method): the suite flags the difference instead of passing silently.
- The mock is pointed at a real address by mistake: the suite refuses to run.
- A fault is left switched on after a failed test: the next reset clears it.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The project MUST provide a mock environment that is started with a single documented command and stopped and reset with another.
- **FR-002**: The mock environment MUST stand in for every external system the function uses: the SyteLine web service, the SQL Server database, the IDP document source, the credentials store, the jobs table, the function's background self-start, the schedule, and the API entry point.
- **FR-003**: The function under test MUST talk to the mock through the same kind of connection it uses in production (network requests to an address, not in-process replacements), so address, protocol, login and encoding handling are exercised.
- **FR-004**: The function MUST run unmodified. The only difference between the mock and production is the addresses and credentials it is given.
- **FR-005**: The mock MUST accept the IDP processing-complete event in the exact shape production sends (the sample event) and return the same response fields.
- **FR-006**: The mock ERP MUST record every call it receives, so a test can assert exactly what would have been written.
- **FR-007**: The mock MUST be able to inject each failure listed in User Story 2 on demand, one at a time or combined, and switch it off again.
- **FR-008**: The mock MUST apply the same settings names, time limit and memory limit as the deployment template, and deny any service call the template does not permit.
- **FR-009**: The mock MUST ship with named data scenarios (for example a document that passes review, one that needs review, one already processed) that tests can select.
- **FR-010**: Resetting the mock MUST return all of its state, including jobs, locks, reservations and injected faults, to a known starting state.
- **FR-011**: The integration suite MUST refuse to run if any address it is given is not a mock address, and MUST never perform a live ERP write.
- **FR-012**: The suite MUST report when the mock and the deployment template or the function's list of actions no longer agree.
- **FR-013**: The suite MUST run unattended, produce a clear pass/fail result and exit code, and finish within a stated time.
- **FR-014**: The mock SQL Server stand-in MUST be a real SQL Server engine, so login, encryption, driver and query dialect are exercised as in production. Starting it may take longer than the other mocks (see the start-up edge case).
- **FR-015**: Mock data MUST be synthetic only. No production document, ERP record or database row, sanitized or not, may be stored in the mock or the repository.
- **FR-016**: Documentation MUST explain what the mock does and does not reproduce, so a pass is not read as proof of production behaviour.

### Key Entities

- **Mock environment**: the set of stand-in services started together, with one address list handed to the function.
- **Scenario**: a named starting state for the mock (documents, ERP records, database rows, jobs) that a test selects.
- **Fault**: a switchable misbehaviour of one mock service (denied, slow, unavailable, wrong answer).
- **Call record**: what a mock service received, kept so tests can assert on it.
- **Fidelity gap**: a documented difference between the mock and production.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A developer with no cloud account can start the environment and complete a full intake run in under 10 minutes from a fresh checkout.
- **SC-002**: A full integration run, from reset to result, finishes in under 5 minutes.
- **SC-003**: Every failure class seen in production so far (IDP read denied, module missing, encryption key denied, document needs review, ERP unreachable) can be reproduced on demand and gives the same operator-facing message as production.
- **SC-004**: Two consecutive runs from a reset state give identical results 100% of the time.
- **SC-005**: Zero calls reach a real ERP, IDP, database or cloud account during a suite run, verified with outside network access blocked.
- **SC-006**: A setting or action added to the deployment is caught by the suite before release in 100% of cases, rather than first failing after deployment.

## Assumptions

- The existing unit tests with in-process stand-ins stay; this feature adds a second, slower layer and replaces none of them.
- Developers run on a laptop with a container runtime available; a pipeline runner has the same.
- The sample event and the example document used so far are the first scenario.
- Live ERP writes stay off, and the standing rule that credentials are never printed or logged applies to the mock as well.
- The mock reproduces behaviour the function can observe (responses, errors, timing limits). It does not reproduce the private internals of the ERP, the IDP or the cloud provider, and that gap is documented (FR-016).
- Real-network checks against the actual worker network remain the existing opt-in read-only checks; this feature does not replace them.
