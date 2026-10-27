# Research: Attach PO Notes to the ERP Records

## R1 - How a note is created
**Decision**: the ERP owner's five steps through the web service: read the order's RowPointer (`SLCos`), insert a `SpecificNotes` row (`NoteDesc`, `NoteContent`), read the new `SpecificNoteToken` from the insert's answer (refresh-after-save returns the stored row), insert the link row with `NoteHeaderToken` 31 (order), `RefRowPointer`, `NoteType` 0.
**Rationale**: it is exactly the owner's tested path (their SQL did the same), and needs no stored-procedure permission. **Alternatives**: the `CreateSpecificNoteSp` method the existing `add_note` uses (creates and attaches in one call, but its IDO and parameters are unconfirmed and it cannot set the title separately from the text); direct SQL (the Lambda's database access is read-only).
**Open**: the link IDO is named `SLObjectNotes` by the owner, `ObjectNotes` in feature 001; default to the owner's name, overridable by `SYTELINE_OBJECT_NOTE_IDO`, and confirm with `probe_properties`/`list`.

## R2 - Getting the token
**Decision**: take `SpecificNoteToken` from the insert's returned row; when the answer lacks it, read `SpecificNotes` for the title and content just written, newest first, one row. If neither gives a token, the note is reported not attached and the link is not guessed.
**Rationale**: the owner's SQL found the token by `CreateDate`; a content/title match is safer than a time window. **Alternative**: a time window on `CreateDate` (can pick up another user's note).

## R3 - Which text, which title
**Decision**: titles "Customer Requirements", "Regulatory Compliance", "Shipping Information", "Billing Information", "Work To Be Performed"; the fields `customer_requests`, `regulatory_compliance`, `shipping_information`, `billing_information`, `work_to_be_performed` of the document result, mapped through `idp_fields` (overridable by `IDP_FIELD_MAP` like the others). `erp_notes_summary` is not attached (spec clarification).
**Rationale**: the field names are in the sample result. **Alternative**: attach the summary as well (rejected by the user).

## R4 - Duplicates
**Decision**: before writing, list the order's existing notes (link rows with `NoteHeaderToken` 31 and the order's RowPointer, then their `SpecificNotes` titles). Normal mode: a title that exists is skipped. Development mode: a title that exists has its `NoteContent` updated (with the `_ItemId` read from feature 004 R20), a missing one is inserted.
**Rationale**: a normal save creates a new order so duplicates only arise on a repeated save; in development the same order is reused on every run. **Alternative**: delete and recreate (rejected: no ERP row is ever deleted).

## R5 - Failure and ordering
**Decision**: the notes run after the order, lines and jobs are written and read back, and before the final state is recorded. Each note is attempted in turn; a definite refusal is a warning and the next note still runs; a possibly-written (uncertain) or unreachable ERP stops the remaining notes and reports them as not attached. The order's outcome is unchanged either way.
**Rationale**: the order and job numbers are the valuable part (FR-007, SC-002).

## R6 - Size
**Decision**: `NoteContent` is sent whole; `NoteDesc` is the short fixed title. If the ERP refuses a long text the note is reported not attached with the ERP's message (never cut). The limit is learned from the first refusal and documented.
**Rationale**: silent truncation was ruled out by the spec.

## R7 - Where it does not apply
**Decision**: the one-step upload path (`work_upload`) is unchanged; only the save workflow adds notes. Live writes off: no note is attempted and the report says so.
