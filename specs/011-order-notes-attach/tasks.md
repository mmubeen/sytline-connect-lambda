---

description: "Task list for attaching PO notes to the ERP order"
---

# Tasks: Attach PO Notes to the ERP Records

**Input**: Design documents from `/specs/011-order-notes-attach/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/note-result.md

**Tests**: included (the repository tests every behaviour with fakes before deployment).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependencies)
- Python tests: `PYENV_VERSION=3.12.14 python -m pytest ...`

---

## Phase 1: Setup

- [X] T001 Add to `src/syteline/idos.py`: `SPECIFIC_NOTE_IDO = "SpecificNotes"` (reuse `NOTE_IDO`), `object_note_link_ido()` returning `SYTELINE_OBJECT_NOTE_IDO` or default `"SLObjectNotes"`, `NOTE_HEADER_CO = 31` (job 115 and line 37 listed as constants for later), `NOTE_TYPE = 0`
- [X] T002 [P] Extend `FakeSoap` in `tests/fakes.py`: an insert into `SpecificNotes` stores the row and returns it with a generated `SpecificNoteToken`; the link IDO stores rows; `SLCos` rows carry a `RowPointer`; options to refuse a note by title and to raise an uncertain error at the n-th insert
- [X] T003 [P] Extend `mock/erp/app.py`: generate `SpecificNoteToken` on `SpecificNotes` inserts and `RowPointer` on `SLCos` inserts

---

## Phase 2: Foundational (blocks all stories)

- [X] T004 Add the five sections to the PO details: in `src/syteline/idp_fields.py` add `note.customer_requests`, `note.regulatory_compliance`, `note.shipping_information`, `note.billing_information`, `note.work_to_be_performed` to `DEFAULT_MAP` (document fields of the same names, overridable by `IDP_FIELD_MAP`), and return `details["notes"]` as an ordered list of `{title, text}` of the non-blank sections (titles: "Customer Requirements", "Regulatory Compliance", "Shipping Information", "Billing Information", "Work To Be Performed"); blank or whitespace-only text yields no entry; the notes never affect `missing` or the fingerprint
- [X] T005 [P] Tests in `tests/unit/test_idp_fields.py`: all five present, some blank/absent, whitespace only, `IDP_FIELD_MAP` override, fingerprint unchanged by notes

**Checkpoint**: details carry `notes`.

---

## Phase 3: User Story 1 - The document's notes appear on the order (Priority: P1) MVP

**Goal**: five separate notes on the customer order after a save.

**Independent Test**: save an order from a document with five sections; the fake ERP holds five `SpecificNotes` rows and five link rows (header token 31, the order's RowPointer, note type 0).

- [X] T006 [P] [US1] Tests in `tests/unit/test_notes.py`: `attach_order_notes` reads the order's RowPointer from `SLCos`, inserts `SpecificNotes` (`NoteDesc` = title, `NoteContent` = full text), takes `SpecificNoteToken` from the returned row, falls back to reading `SpecificNotes` by title and content when the answer has no token, inserts the link row with `NoteHeaderToken` 31, `RefRowPointer`, `NoteType` 0; no non-blank section means nothing is written
- [X] T007 [US1] Implement `attach_order_notes(soap, co_num, sections, replace=False)` in `src/syteline/notes.py` returning the report of `contracts/note-result.md` (`attempted`, `attached`, `not_attached`, `skipped_existing`, `replaced`); order's RowPointer from `SLCos` with the padded `CoNum` filter (`rowbuild.co_filter`); no token means the note is `not_attached` and no link is written; the note text is never put in the report or in logs
- [X] T008 [US1] Call it from the save in `src/syteline/savewf.py` after the read-back confirmed the order and before the final state is recorded (both the normal path and `_overwrite`): `sections = req.get("notes") or []`; put the report in the outcome as `notes`; the outcome `state` is not changed by it
- [X] T009 [P] [US1] Test in `tests/unit/test_savewf.py`: a save with five sections gives five notes on the order only (none on lines or jobs), the outcome has `notes.attached` with five titles, and the one-step upload path (`work_upload`) writes none

**Checkpoint**: US1 works with fakes.

---

## Phase 4: User Story 2 - A note problem never loses or hides the order (Priority: P2)

**Goal**: a refused or failed note is a warning, never a failed order.

**Independent Test**: refuse one note; the order stays saved, the note is listed with a reason, the others are attached.

- [X] T010 [P] [US2] Tests in `tests/unit/test_notes.py`: a definite refusal is `not_attached` with the ERP's message and the next note still runs; an uncertain error stops the remaining notes, marks the failing one `uncertain: true` and the rest `not attached` with no retry (insert called once); unreachable ERP behaves the same; live writes off gives `attempted: false` and no call; a missing RowPointer gives every note `not_attached` with the reason
- [X] T011 [US2] In `src/syteline/notes.py` classify errors: `ErpRejected` is a per-note warning and continues; `ErpUncertain`/`ErpUnreachable` stop the rest; `LiveWritesDisabled` gives `attempted: false`; never retry
- [X] T012 [US2] In `src/syteline/savewf.py` add one outcome warning per `not_attached` note (`note "<title>" was not attached: <reason>`) and skip the notes with a warning when `remaining time` is below the minimum used by the other steps
- [X] T013 [P] [US2] Test in `tests/unit/test_savewf.py`: with a refused note the outcome `state` is still `succeeded`, the warnings name the note, and the order/line/job rows are unchanged; with an uncertain error the state is still `succeeded` and the report says `uncertain`

---

## Phase 5: User Story 3 - No duplicate notes (Priority: P3)

**Goal**: one copy of each note per order; development runs replace the text.

**Independent Test**: run the same save twice against the same target; each title appears once.

- [X] T014 [P] [US3] Tests in `tests/unit/test_notes.py`: existing notes are listed through the link rows (`NoteHeaderToken` 31 and the RowPointer) and their `SpecificNotes` titles; normal mode skips an existing title (`skipped_existing`); `replace=True` updates the existing note's `NoteContent` through `client.update` (with `_ItemId`) and reports it in `replaced`; a missing title is inserted in both modes; nothing is deleted
- [X] T015 [US3] Implement the existing-notes read and the skip/replace branches in `attach_order_notes`; `savewf` passes `replace=True` when `self.dev` is set
- [X] T016 [P] [US3] Test in `tests/unit/test_savewf.py`: ten development runs against one target leave five notes (SC-004); a repeated normal save does not add a second set (SC-003)

---

## Phase 6: Polish and Cross-Cutting

- [X] T017 [P] Update `docs/SAVE_WORKFLOW.md` with "The order notes": the five titles, the five-step path, the link IDO setting, the duplicate rule, the report fields, and "not done: lines, jobs, summary"
- [X] T018 [P] `SYTELINE_OBJECT_NOTE_IDO`: not added to `src/template.yaml` / `src/deploy.sh` / `src/deploy.env.example`: the existing note IDO settings (`SYTELINE_NOTE_METHOD_IDO`) are not listed there either; the variable is read from the function environment and documented in `docs/SAVE_WORKFLOW.md`
- [X] T019 Run the full suite (`PYENV_VERSION=3.12.14 python -m pytest tests -q --deselect tests/unit/test_sample_event.py --ignore=tests/unit/mock/test_make_guards.py`) and fix regressions
- [ ] T020 [P] Mock-environment run of the notes (needs Docker; mark as not run when unavailable)
- [ ] T021 Deployment-only: `probe_properties` with `list` on `SpecificNotes` and the link IDO to confirm names (`quickstart.md` step 2), then a development run with five sections and a repeat run; record the longest accepted note text if a refusal occurs

---

## Dependencies and Execution Order

- Phase 1 then Phase 2, which blocks everything.
- US1 (T006-T009) first; US2 and US3 extend `notes.py` and `savewf.py` and follow US1 (same files, so sequential).
- Polish after the stories.

### Parallel opportunities

- T002, T003 together; T005 with T004's tests; T006 with T009; T010 with T013; T014 with T016; T017, T018, T020 together.

## Implementation Strategy

MVP is Phase 1-3 (five notes on the order). Then US2 (never harms the order), then US3 (no duplicates). T021 is verified once, at deployment.
