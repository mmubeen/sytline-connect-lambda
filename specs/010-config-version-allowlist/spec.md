# Feature Specification: Process Only Allowed Document Configurations

**Feature Branch**: `010-config-version-allowlist`

**Created**: 2026-10-26

**Status**: Draft

**Input**: User description: "the lambda should restrict processing to a limited set of config_version within detial.input"

## Background

The document-processing (IDP) solution runs several document configurations side by side; the configuration a document was processed with is recorded in the finished-processing event, inside the event's `detail.input`, as the document's `config_version` (for example `ChromAlloy-PO`, a purchase-order configuration). Today the integration acts on **every** finished document that has a document ID: it reads the result, resolves a customer and reserves CO and job numbers. A document processed with any other configuration (an invoice, a test configuration, another customer's) would be treated as a purchase order, consuming ERP numbers and, once live writes are on, creating orders from documents that are not purchase orders.

This feature makes the deployment name the **set of configuration versions it will process**. An event whose `config_version` is not in the set is ignored before the document is read, a number is reserved or the ERP is contacted.

## Clarifications

### Session 2026-10-26

- Q: What happens to an event that is not allowed? → A: (assumed default) It is ignored, like any other event that is not meant for this integration: reported as `ignored` with the reason and the version found, nothing read or recorded, no error.
- Q: What does an empty allowed set mean? → A: (assumed default) No restriction, as today, so existing deployments are unchanged until they set it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Only purchase-order documents are processed (Priority: P1)

The deployer lists the configuration versions to process (for example `ChromAlloy-PO`). A finished event whose `detail.input` carries one of them is processed exactly as today. An event carrying any other version is ignored: no document is read, no customer matched, no number reserved, no ERP call, nothing recorded in the job tracker.

**Why this priority**: it is the whole purpose; without it, any document in the shared IDP solution can start an order.

**Independent Test**: set the allowed list to `ChromAlloy-PO`; send the sample event (version `ChromAlloy-PO`): it is processed. Change its version to `Other-Config` and send it: the answer is `ignored`, naming the version, and no IDP, ERP or tracker call is made.

**Acceptance Scenarios**:

1. **Given** the allowed set is `ChromAlloy-PO`, **When** an event with `config_version` `ChromAlloy-PO` arrives, **Then** it is processed as today.
2. **Given** the same set, **When** an event with `config_version` `Invoice-v2` arrives, **Then** the answer is `ignored` with a reason that names the version found and the integration touches nothing else.
3. **Given** the set has several versions (`ChromAlloy-PO`, `ChromAlloy-PO-2`), **When** an event with any one of them arrives, **Then** it is processed.
4. **Given** the allowed set is empty, **When** any event arrives, **Then** it is processed as today.

---

### User Story 2 - A missing or unreadable version never slips through (Priority: P1)

When a restriction is set, an event whose `detail.input` has no `config_version`, or whose version is not a short text value, is treated as not allowed. Nothing is guessed from other parts of the event.

**Why this priority**: a restriction that lets unlabelled documents through is no restriction.

**Independent Test**: with the set `ChromAlloy-PO`, send events with no version, an empty version, a number, and a very long text: each is ignored and nothing is touched.

**Acceptance Scenarios**:

1. **Given** a restriction is set, **When** `detail.input` has no `config_version`, **Then** the event is ignored with the reason "no config_version".
2. **Given** a restriction is set, **When** `config_version` is empty, not text, or longer than the allowed length, **Then** the event is ignored and the value is not echoed beyond a short, safe form.
3. **Given** no restriction is set, **When** the version is missing, **Then** the event is processed as today (the version is not required).

---

### User Story 3 - The deployer can see and change what is allowed (Priority: P2)

The allowed set is a deployment setting, validated before deployment, shown by the deployment output or warning when it is empty, and reported in the integration's connectivity/health answer so the deployer can confirm what is in force. Changing it needs only a redeploy.

**Why this priority**: an invisible filter is a support burden: "why was my document ignored?" must be answerable from the answer and the logs.

**Independent Test**: deploy with a malformed list: refused before anything is created. Deploy with `ChromAlloy-PO`: the health answer lists it; a skipped event's log line carries the version found and the allowed set.

**Acceptance Scenarios**:

1. **Given** a malformed setting (empty items, illegal characters), **When** the deployment is attempted, **Then** it is refused with a message naming the problem.
2. **Given** the setting is empty, **When** the deployment is made, **Then** the deployer sees a notice that every configuration will be processed.
3. **Given** an event was ignored for its version, **When** the logs are read, **Then** the line shows the version found and the allowed set (no document content).

---

### Edge Cases

- Matching is exact and case-sensitive on the whole value (`chromalloy-po` is not `ChromAlloy-PO`); surrounding spaces in the setting's items are trimmed, never in the event's value.
- An event that is not a finished execution, or has no document ID, is handled as today, before and independent of the version check.
- Both `detail.input` and `detail.output` carry a version and they differ: the **input** one decides (the spec's stated source); the output one is not consulted.
- `detail.input` is too large or not valid JSON: handled as today (refused as invalid), the version check never rescues it.
- Development mode (`DEV_TARGET_CO`) does not bypass the restriction.
- The direct reserve/save requests (which name a document ID, not an event) are not events and are not subject to this filter.
- A version is allowed today and removed later: documents already reserved or saved are unaffected; only new events are filtered.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The deployer MUST be able to set the allowed configuration versions as a list; empty means no restriction.
- **FR-002**: With a restriction set, an event MUST be processed only when the `config_version` found in `detail.input` equals one of the allowed versions, matching the whole value exactly.
- **FR-003**: An event that is not allowed MUST be answered `ignored`, with a reason naming the version found (or that none was found), and MUST NOT read the document, contact the ERP, reserve or write anything, or create any job-tracker entry.
- **FR-004**: With a restriction set, an event with no `config_version`, an empty one, a non-text one or an over-long one MUST be treated as not allowed.
- **FR-005**: The check MUST run after the event is recognised as a finished-processing event with a document ID and before anything else is done with the document; events that fail those earlier tests behave as today.
- **FR-006**: With no restriction set, behaviour MUST be exactly as today, whether or not the event has a version.
- **FR-007**: Only `detail.input` decides; the version in `detail.output` MUST NOT be used.
- **FR-008**: The setting MUST be validated before deployment (each item 1 to 64 letters, digits, `.`, `_`, `-`, no empty items) and a malformed value MUST be refused before anything is created.
- **FR-009**: The deployment MUST notify the deployer when the set is empty, and the health/connectivity answer MUST list the allowed versions in force.
- **FR-010**: Logs and answers for an ignored event MUST carry the version found (in a short, safe form) and the allowed set, and MUST NOT carry any other event or document content.
- **FR-011**: Development mode and the direct reserve and save requests MUST be unaffected by, and MUST NOT bypass, this check as described in the edge cases.

### Key Entities

- **Allowed configuration versions**: the deployment's list of document configurations it will process; may be empty.
- **Configuration version of an event**: the `config_version` text found inside the event's `detail.input`; absent, empty or invalid counts as "not found".
- **Ignored-for-version answer**: the `ignored` outcome with the version found and the allowed set.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With a restriction set, 100% of events carrying a version outside the set are ignored and cause no document read, ERP call, number reserved or tracker entry.
- **SC-002**: 100% of events with an allowed version are processed exactly as before the feature (all existing event tests pass unchanged with an allowing setting or none).
- **SC-003**: 100% of events with a missing, empty or malformed version are ignored when a restriction is set.
- **SC-004**: A deployer can tell from one log line or one answer why a document was ignored, and what is currently allowed.
- **SC-005**: A malformed setting is refused before deployment in 100% of attempts.

## Assumptions

- The version lives at `document.config_version` inside the JSON text of `detail.input`, as in the sample event (`ChromAlloy-PO`); `config_revision` is not used.
- The set is a deployment setting changed by redeploying; there is no per-request override and no pattern or wildcard matching.
- The `ignored` outcome is a success from the caller's point of view (no error, no retry), the same as other events not meant for this integration.
- The input text is read only as far as needed to find the version; the existing document-ID parsing of `detail.input` is reused.
- Existing deployments keep working unchanged until they set the list.
