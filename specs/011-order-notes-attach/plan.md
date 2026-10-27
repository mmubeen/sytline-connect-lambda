# Implementation Plan: Attach PO Notes to the ERP Records

**Branch**: `011-order-notes-attach` | **Date**: 2026-10-26 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/011-order-notes-attach/spec.md`

## Summary

After the save has created (or, in development mode, overwritten) the customer order and read it back, the five free-text sections of the processed document (customer requests, regulatory compliance, shipping information, billing information, work to be performed) are attached to the **order only** as five separate notes. Each note is made the way the ERP owner described: read the order's RowPointer from `SLCos`, insert a `SpecificNotes` row (`NoteDesc`, `NoteContent`), take its `SpecificNoteToken`, insert the link row (`SpecificNoteToken`, `NoteHeaderToken` 31, `RefRowPointer`, `NoteType` 0). A note step never changes the order's outcome: failures become warnings and a per-note report; an unknown outcome is never retried. Duplicates are avoided by title (skip when present; in development mode the existing note's text is replaced).

## Technical Context

**Language/Version**: Python 3.13 (tests on 3.12), AWS SAM Lambda (GovCloud)

**Primary Dependencies**: existing `zeep`/`lxml` SOAP client (`client.insert/update/load`), `idp_fields` mapping; no new dependency

**Storage**: none new; the note text is read from the IDP document each time and never stored in DynamoDB or logged

**Testing**: pytest with `FakeSoap` (extended with `SpecificNotes`/link tables) and the mock ERP

**Target Platform**: the single existing Lambda (worker `work_save` path)

**Project Type**: serverless integration function

**Performance Goals**: at most 5 notes x 3 ERP calls (about 15 calls) added to a save, inside the existing time budget; notes are skipped, with a warning, when too little time is left

**Constraints**: writes only with live writes on; note text never logged; a note failure never fails the order; no automatic retry after an unknown outcome

**Scale/Scope**: 5 notes per order, text up to the ERP's note capacity

## Constitution Check

`.specify/memory/constitution.md` is still the unfilled template, so there are no project gates. The standing rules of this repository are applied instead: live ERP writes off by default (honoured), no secrets or document text in logs (honoured), uncertain writes never retried (honoured), no deletion of ERP rows (honoured: replacement in development mode updates the note, it never deletes). **Result: pass.**

## Project Structure

### Documentation (this feature)

```text
specs/011-order-notes-attach/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/note-result.md
└── tasks.md             # /speckit-tasks
```

### Source Code (repository root)

```text
src/syteline/
├── idp_fields.py   # + the five note sections in DEFAULT_MAP (note.*) and `notes` in the details
├── idos.py         # + SPECIFIC_NOTE_IDO, OBJECT_NOTE_LINK_IDO (default per the ERP owner), CO note-header token 31
├── notes.py        # + attach_order_notes(soap, co_num, sections, replace) -> report (the 5-step path)
├── savewf.py       # + a notes step after the read-back; report in the outcome and warnings
└── errors.py       # (no new fatal code; notes only warn)
tests/unit/         # test_notes.py (extended), test_idp_fields.py, test_savewf.py
tests/fakes.py      # FakeSoap: SpecificNotes / link tables, token generation
mock/erp/app.py     # token generation for SpecificNotes inserts
docs/SAVE_WORKFLOW.md
```

**Structure Decision**: extend the existing modules; no new package.

## Complexity Tracking

No constitution violations to justify.
