# Contract: notes in the save outcome

The save outcome (reservation status and worker result) gains an optional `notes` object. Existing fields are unchanged.

```json
"notes": {
  "attempted": true,
  "attached": ["Customer Requirements", "Work To Be Performed"],
  "not_attached": [{"title": "Billing Information", "reason": "ERP refused: ...", "uncertain": false}],
  "skipped_existing": [],
  "replaced": []
}
```

- `attempted` is false (and the rest empty) when live writes are off, the document has no non-blank section, or no time was left; a `warnings` entry says which.
- `not_attached[].uncertain` is true when the ERP may have received the note; that note and all later ones are not retried.
- Every entry in `not_attached` also adds one line to the outcome's `warnings`.
- The outcome's `state` is never changed by notes.
- No note text appears in the outcome or in the logs.
