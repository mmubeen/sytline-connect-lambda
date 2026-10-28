# Feature Specification: Due Date from the Customer Contract

**Feature Branch**: `012-due-date-from-contract`

**Created**: 2026-10-28

**Status**: Draft

**Input**: User description: "Calculate due date as per demo code and update ERP with value based on customer contract or item master defaults"

## Clarifications

### Session 2026-10-28

- Q: When the PO document itself carries a due date for a line, which due date goes to the ERP? → A: The computed one wins, as in the demo; a date read from the document is ignored.
- Q: Should the unit of measure also come from the customer contract? → A: No. This feature changes only the due date; the unit of measure stays as the document gives it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Each order line gets its due date from the customer's contract (Priority: P1)

When the save writes an order line, its due date is the date the PO was received plus the number of days the customer's contract allows for that item (its "due period"), so the line carries the delivery date the customer agreed to instead of a blank or a date copied from the paper.

**Why this priority**: The due date drives the job's schedule and what the shop floor promises the customer; without it every line has to be dated by hand.

**Independent Test**: Save an order for a customer and item that have a contract due period of 30 days, received on a known date; the line in the ERP shows the received date plus 30 days.

**Acceptance Scenarios**:

1. **Given** the customer has a contract for the line's item with a due period, **When** the save writes the line, **Then** the line's due date is the received date plus that many days.
2. **Given** the customer's contract has no due period for the item (none, or zero), **When** the save writes the line, **Then** the ERP's company-wide default due period is used instead.
3. **Given** the document itself shows a due date for the line, **When** the save writes the line, **Then** the computed date is used and the document's date is ignored.
4. **Given** neither the contract nor the company default gives a due period, **When** the save writes the line, **Then** the line is written without a due date, and the result carries a warning naming the line.

---

### User Story 2 - The job's schedule follows the same date (Priority: P2)

The job created for a line starts and ends on that line's due date, as it does for the demo, so the order line and its job agree.

**Why this priority**: A job dated differently from its order line misleads planning.

**Independent Test**: Save an order whose line gets a computed due date; the line's job shows that same date as its scheduled start and end.

**Acceptance Scenarios**:

1. **Given** a line with a computed due date, **When** its job is written, **Then** the job's scheduled start and end are that date.
2. **Given** a line without a due date, **When** its job is written, **Then** the job's start and end fall back to the job date, as they do today.

---

### User Story 3 - A lookup problem never fails the save (Priority: P3)

Reading the contract or the default can fail or return nothing. That must not stop the order from being saved.

**Why this priority**: The order and job numbers are the valuable, hard-to-redo part; a missing date can be fixed later.

**Independent Test**: Make the contract lookup fail; the order still saves, the line has no due date, and the result says why.

**Acceptance Scenarios**:

1. **Given** the ERP refuses the contract or default lookup, **When** the save runs, **Then** the order is still saved, the affected lines have no due date, and the result lists a warning for each.
2. **Given** the ERP is unreachable during the lookups, **When** the save runs, **Then** it behaves as it does for any other unreachable-ERP case; no due date is guessed.

---

### Edge Cases

- The received date is missing or not a valid date: no due date can be computed; the line is written without one, with a warning.
- The due period is not a whole positive number (text, zero, negative): treated as no due period for that source.
- Several lines of one order share an item: the lookup result may be reused; each line still gets its own date.
- Development mode (a fixed target order and job): the same rule applies and the existing line and job are updated with the computed date.
- Live ERP writes are switched off: nothing is written, as for every other write; the computed date appears only in the dry result, if there is one.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: For each order line the save writes, the system MUST compute the due date as the order's received date plus a due period in days.
- **FR-002**: The due period MUST be taken from the customer's contract for that customer and item when it holds a positive whole number of days.
- **FR-003**: When the contract gives no usable due period, the system MUST use the ERP's company-wide default due period when that is a positive whole number of days.
- **FR-004**: When neither source gives a usable due period, or the received date is not usable, the line MUST be written without a due date, and the result MUST carry a warning naming the line and the reason.
- **FR-005**: The computed due date MUST replace any due date read from the document; the document's date is not written.
- **FR-006**: The computed due date MUST be written to the order line and MUST be the scheduled start and end of the line's job; a line without a due date keeps today's job-date fallback.
- **FR-007**: The unit of measure MUST NOT change: it stays as the document gives it.
- **FR-008**: A refused or failed lookup MUST NOT change the outcome of the save: the order stays saved and the affected lines are written without a due date and with a warning. An unreachable ERP is handled as for any other read.
- **FR-009**: The result MUST report, per line, the due date written and where it came from (contract, company default, or none).
- **FR-010**: The lookups are reads only; they MUST be made before the line is written, and nothing about them may be logged beyond the customer, item, days and source.

### Key Entities

- **Due period**: A number of days a customer's contract (per item) or the company default allows between receiving a PO and delivering.
- **Customer contract entry**: The ERP's record tying a customer and an item to its due period.
- **Company default**: The ERP's company-wide due period, used when the contract gives none.
- **Computed due date**: The received date plus the due period; written to the line and the job.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For every line whose customer and item have a contract due period, 100% of saved lines show the received date plus that period as the due date.
- **SC-002**: For every line without a contract period but with a company default, 100% of saved lines show the received date plus the default.
- **SC-003**: Every line whose due date could not be computed is listed in the result with a reason; none is dated silently.
- **SC-004**: A failed or refused lookup never turns a saved order into a failed one in any tested case.
- **SC-005**: In every saved order, each job's scheduled start and end equal its line's due date (or the job date when the line has none).

## Assumptions

- "Item master defaults" in the request is read as the demo does: the customer-and-item contract entry first, then the ERP's company-wide due period. If the item master itself also holds a due period of its own, that is a later addition.
- The received date is the order's received date already used for the order date; the due date is that date plus whole days, written as the ERP's usual date format with the Eastern offset.
- The demo's lookup order and rounding are followed exactly: contract with a positive period, else company default, else none.
- The teardown date (a separate date, received date plus 7 days in the demo) is a different feature and is not changed here.
- The PO OCR demo's order-line step is the reference for the rules above; where this spec and the demo differ, the demo's behavior wins unless stated.
- The names of the contract and default records' fields are those the demo uses; they are confirmed against the ERP before release, as for earlier features.
