# Feature Specification: Attach PO Notes to the ERP Records

**Feature Branch**: `011-order-notes-attach`

**Created**: 2026-10-26

**Status**: Draft

**Input**: User description: "Fields in result.json: customer_requests, regulatory_compliance, shipping_information, billing_information, work_to_be_performed, erp_note_summary (combines the above 5 into 1). Steps: create the core record (job, customer order or order line), get its RowPointer, create a SpecificNotes record (NoteDesc = title, NoteContent = full text), get its SpecificNoteToken, then create the ObjectNotes record tying the note to the core record (SpecificNoteToken, NoteHeaderToken 115 for job / 31 for CO / 37 for CO line, RefRowPointer, NoteType 0)."

## Clarifications

### Session 2026-10-26

- Q: Which records receive the notes? → A: The customer order only.
- Q: Five separate notes, one summary, or both? → A: The five separate notes (no summary).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The document's notes appear on the order (Priority: P1)

After the save has created the customer order, its lines and jobs, the free-text sections the document processing extracted (customer requests, regulatory compliance, shipping information, billing information, work to be performed) are attached to the ERP records as notes, so the people working the order see the customer's instructions in the ERP without opening the PO.

**Why this priority**: These instructions are why the notes exist; without them the order is created but the customer's requirements stay on paper.

**Independent Test**: Save an order from a document that has all five sections; open the order in the ERP and see one note per section, each with its title and full text.

**Acceptance Scenarios**:

1. **Given** a document with all five sections, **When** the save completes, **Then** each section is attached as a separate note titled with its section name ("Customer Requirements", "Regulatory Compliance", "Shipping Information", "Billing Information", "Work To Be Performed") holding the section's full text.
2. **Given** a document where some sections are empty or absent, **When** the save completes, **Then** only the sections that have text are attached; no empty notes are created.
3. **Given** the document has a combined summary of the five sections, **When** the save completes, **Then** the summary is not attached; only the five separate notes are.

---

### User Story 2 - A note problem never loses or hides the order (Priority: P2)

Notes are added after the order exists. If a note cannot be added, the order, its lines and its jobs stay saved and the result says exactly which notes were not attached and why.

**Why this priority**: The order and job numbers are the valuable, hard-to-redo part; a failed note must not make them look failed or be written twice.

**Independent Test**: Make the ERP refuse one note; the save result still reports the order as saved, lists the refused note by title with the reason, and the other notes are attached.

**Acceptance Scenarios**:

1. **Given** one note is refused by the ERP, **When** the save completes, **Then** the order is reported saved with a warning naming that note, and the remaining notes are still attempted.
2. **Given** the ERP becomes unreachable partway through the notes, **When** the save completes, **Then** the order stays saved, the notes already attached are listed, the rest are listed as not attached, and nothing is retried automatically.

---

### User Story 3 - Development runs do not pile up duplicate notes (Priority: P3)

In development mode the same target order is overwritten on every run. The notes from the document replace the earlier ones for that run rather than adding a second copy each time.

**Why this priority**: Repeated test runs against one order would otherwise leave dozens of identical notes on a production record.

**Independent Test**: Run a development save twice against the same target; the order shows each note once.

**Acceptance Scenarios**:

1. **Given** a target order that already carries notes from an earlier run of the same document, **When** a development save runs again, **Then** each note appears once, with the text of the latest run.

---

### Edge Cases

- A section's text is longer than a single note can hold: the full text is kept, split across notes if needed, or the note is refused with a clear warning (never silently cut).
- A section contains only whitespace: treated as empty, no note.
- The order's RowPointer is not found after saving: its notes are skipped with a warning; the order stays saved.
- Live ERP writes are switched off: no note is written, as for every other write.
- The same document is sent again: no second set of notes is added to an order that already has them (except in development mode, User Story 3).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: After a save has created or updated the order, its lines and its jobs, the system MUST attach the document's text sections as notes to the ERP records: customer requests, regulatory compliance, shipping information, billing information and work to be performed.
- **FR-002**: Each note MUST carry a title (the section's name) and the section's full text; only sections with non-blank text produce a note.
- **FR-003**: The notes MUST be attached to the customer order only (not to its lines or jobs). The note link supports the record types job, customer order and customer order line, so attaching to the others can be added later.
- **FR-004**: The five sections MUST be attached as five separate notes. The document's combined `erp_notes_summary` is NOT attached.
- **FR-005**: A note MUST be tied to its record through the ERP's own note tables: a note is created, then linked to the record by the record's RowPointer with the record-type code (job 115, customer order 31, order line 37) and note type 0.
- **FR-006**: Notes MUST be added only after the record exists and its RowPointer can be read; a record whose RowPointer is not found gets no notes and a warning.
- **FR-007**: A refused or failed note MUST NOT change the outcome of the order: the order stays saved, the result lists every note not attached with its title and the reason, and remaining notes are still attempted.
- **FR-008**: A note whose outcome is unknown (the ERP may have received it) MUST NOT be retried automatically, and the result MUST say so.
- **FR-009**: The same document MUST NOT get a second set of notes on an order that already has them; in development mode each run MUST leave one copy of each note on the target.
- **FR-010**: Notes MUST be written only when live ERP writes are enabled; otherwise nothing is written and the result says notes were not attempted.
- **FR-011**: The result MUST report the notes attached (titles) and the notes not attached; the note text itself MUST NOT be written to logs.

### Key Entities

- **Note section**: One of the document's text sections (name, full text, possibly empty).
- **Note**: A titled piece of text stored in the ERP (title, content).
- **Note link**: The tie between a note and one ERP record (the record, its record-type code, note type 0).
- **Target record**: The customer order, order line or job a note is attached to.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For a document with all five sections, 100% of non-empty sections are visible as notes on the order in the ERP after the save.
- **SC-002**: A refused or failed note never changes a saved order to failed: in every tested failure the order and job numbers are still reported as saved.
- **SC-003**: Sending the same document twice leaves exactly one set of notes on the order (zero duplicates).
- **SC-004**: Ten consecutive development runs against one target leave exactly one copy of each note.
- **SC-005**: The result of every save lists, for the order, which notes were attached and which were not, with a reason for each that was not.

## Assumptions

- The note sections come from the same processed document the order is built from; the field names are those of the document result (`customer_requests`, `regulatory_compliance`, `shipping_information`, `billing_information`, `work_to_be_performed`, `erp_notes_summary`).
- The ERP's note IDOs (SpecificNotes for the note, the object-notes IDO for the link) are used through the web service like the other writes; the exact IDO and property names are confirmed against the ERP, as the earlier notes feature's names were.
- The existing add-note and get-notes actions (feature 001) stay as they are; this feature adds notes automatically as part of the save.
- Titles of the notes are fixed English names, one per section.
- The test record the user offered (job 337, RowPointer 6E08D287-1F8D-48C0-8D8D-F41239B613E4) may be used for a read/attach trial of the note link (job code 115); no other existing record is touched.
