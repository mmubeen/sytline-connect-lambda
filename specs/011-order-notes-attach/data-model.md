# Data Model: Attach PO Notes to the ERP Records

Nothing new is stored by this function. The note text is read from the IDP document on each save.

## NoteSection (in the PO details)
| Field | Meaning |
|---|---|
| `title` | Fixed: Customer Requirements / Regulatory Compliance / Shipping Information / Billing Information / Work To Be Performed |
| `text` | The document's text; blank or missing means no note |

`details["notes"]` is an ordered list of `{title, text}` of the non-blank sections (max 5).

## ERP rows written (per note)
| IDO | Properties |
|---|---|
| `SpecificNotes` | `NoteDesc` (title), `NoteContent` (text); returns `SpecificNoteToken` |
| link IDO (`SLObjectNotes`, R1) | `SpecificNoteToken`, `NoteHeaderToken` = 31 (order; 115 job, 37 line reserved), `RefRowPointer` (the order's RowPointer), `NoteType` = 0 |

## Note report (in the save outcome)
`notes: {attempted: bool, attached: [title], not_attached: [{title, reason, uncertain?}], skipped_existing: [title], replaced: [title]}`. Text is never included.

## State
No new state. A note failure adds warnings to the outcome; the reservation/order states are unchanged.
