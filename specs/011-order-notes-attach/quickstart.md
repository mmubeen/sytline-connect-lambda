# Quickstart: validate the notes

1. **Unit**: `PYENV_VERSION=3.12.14 python -m pytest tests/unit/test_notes.py tests/unit/test_idp_fields.py tests/unit/test_savewf.py -q`.
2. **Property check (read-only)**: `{"action": "probe_properties", "ido": "SpecificNotes", "list": true}` and the same for the link IDO; confirm `NoteDesc`, `NoteContent`, `SpecificNoteToken`, `NoteHeaderToken`, `RefRowPointer`, `NoteType` exist. Set `SYTELINE_OBJECT_NOTE_IDO` if the link IDO is not `SLObjectNotes`.
3. **Development run**: with `DevTargetCo` set, send a document that has all five sections. In the ERP the target order shows five notes with their titles; the outcome's `notes.attached` lists five titles.
4. **Repeat**: send the same document again. The order still shows five notes (`notes.replaced` lists five titles).
5. **Failure**: (mock) make one note refuse; the outcome is still saved and `notes.not_attached` names it.
