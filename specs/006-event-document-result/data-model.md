# Data Model: Document Result from a Processing-Complete Event

All items live in the existing jobs table (feature 001/004), under new key prefixes. No PO detail (customer, PO number, lines, field values) is stored (feature 004 FR-021).

## Event (input, not stored)
`source` = `aws.states`; `detail-type` = "Step Functions Execution Status Change"; `time`; `detail.executionArn`, `detail.name`, `detail.status`, `detail.input` and `detail.output` (JSON text or object of `{"document": {"id": ...}}`).

## EVTDOC#<document_id> (once per document, outcome)
| Field | Rule |
|---|---|
| `pk` | `EVTDOC#<document_id>`; created by a conditional put (fails if present) |
| `document_id` | 1 to 256 characters of letters, digits, `. _ - /`; no `..` |
| `execution` | `detail.name` of the claiming event (at most 256 characters) |
| `state` | `processing` -> `reserved` / `save_accepted` / `finished`; deleted again when nothing was reserved (R5) |
| `outcome` | code of the last outcome (`reserved`, `save_accepted`, `refused:<CODE>`, `read_only:<why>`) |
| `reservation_id` | set once a reservation exists |
| `claimed_at`, `updated_at` | epoch seconds |
| `ttl` | claimed items only; same retention as reservations |

State transitions: `processing` -> `reserved` (numbers obtained) -> `save_accepted` (worker started). A refusal before any number: item deleted, `last_outcome` kept in the log only.

## DOCRES#<document_id> (marker)
`pk` `DOCRES#<document_id>`, `reservation_id`, `created_at`; written (unconditional put) by `create_reservation` for every reservation, read by the event path (R5).

## Reservation (feature 004, unchanged)
`request_ref` = `event-<document_id>` for event-started reservations; `uploaded_by` = `event-intake`; `caller_identity` = `event-intake:<execution name>` (FR-006c).

## Scan Result Summary (response only)
`document_id`, `state`, `class`, `pages`, `issues` (count), `fields`: list of `{path, value, confidence, meets_threshold}`, `below_threshold`: list of paths. Not stored, not logged.
