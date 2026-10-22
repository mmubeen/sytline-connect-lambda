# Data Model: Job Number Reservation and Save Workflow

Stored in the existing DynamoDB jobs table (partition key `pk`, TTL attribute `ttl`).

## Reservation  (`pk = RESERVATION#<reservation_id>`)
| Field | Meaning |
|---|---|
| reservation_id | UUID returned to the caller |
| request_ref | caller's idempotency key (1-128 chars) |
| payload_hash | SHA-256 of the canonical reserve request (detects ref reuse with a different payload) |
| fingerprint | SHA-256 of normalized customer name and address, PO number, line count (binds save to this PO) |
| po_number, line_count | as requested (line_count 1..25) |
| uploaded_by, caller_identity | confirming operator; caller principal (session name removed for assumed roles) |
| customer_match | `{cust_num, cust_seq}` resolved at reserve time; the only source of the customer on save |
| co_num | reserved CO number (set when obtained) |
| job_numbers | list in line order: `{line, job}`, appended as each is obtained |
| state | see below |
| failure | `{code, message, step}` when failed / needs_reconciliation / partly_saved |
| save | present once a save was accepted: `{requested_at, payload_hash, request}` (the request is what the worker saves) |
| results | per-line results, top level, recorded by the save run |
| erp_records | `{possibly_written: [{kind, key}]}` while saving and on needs_reconciliation |
| attempts, created_at, updated_at, ttl | as in the upload job; ttl set only once terminal |

`results[]` (one per line, line order): `{line, job, co_line?, item, status: saved|failed|not_attempted|unverified, warnings: [..], error?}`. `unverified` = rows were sent but not confirmed by read-back (a record may exist).

## Request index  (`pk = RESREF#<request_ref>`)
`{request_ref, reservation_id, payload_hash}`, created atomically with the reservation (conditional on not existing). Same ref and hash returns the existing reservation; same ref and different hash is `REQUEST_REF_CONFLICT`.

## States and transitions
```
reserving ──► reserved ──► save_accepted ──► saving ──► saved
    │             │                              ├──► partly_saved
    │             │                              └──► needs_reconciliation
    ├──► failed   └── (never expires; unused numbers reported)
    └──► failed (reserving never finished: read as failed, numbers recorded so far consumed)
```
- `reserving → reserved`: all numbers obtained. `reserving → failed`: validation passed but the ERP failed, live writes off, or time ran out; numbers already obtained stay in `job_numbers` and are reported consumed and unused.
- `reserved → save_accepted`: conditional (only from `reserved`), checks fingerprint, caller, `uploaded_by`; this is what makes a reservation single-use.
- `save_accepted → saving`: worker claim (conditional); the sweeper may re-drive `save_accepted` because no write can have happened.
- `saving → saved`: every line verified by read-back. `→ partly_saved`: definite rejection with the failed line's rows proven absent. `→ needs_reconciliation`: uncertain outcome, unproven absence, read-back mismatch, or stale run.
- Nothing ever leaves `needs_reconciliation`; no state replays a save.

## Derived report fields (status view)
- `numbers`: `{co_num, jobs:[{line, job, usage}]}` where `usage` is `reserved` (not yet saved), `used` (line saved and confirmed), `consumed_unused` (reservation failed, or the line was proven not written), or `unverified` (the save needs reconciliation and this line was not confirmed: a record may exist).
- `warnings`: per-line list (teardown).

## Validation rules
- Reserve: `request_ref`, `confirmed: true`, `uploaded_by`, `customer` (name required, address fields optional strings, no customer-number keys), `po_number` (non-empty string), `line_count` (integer 1..25). All problems reported together.
- Save: same customer and PO number as reserved (via fingerprint), `lines` count equals `line_count`, each line `item`, `quantity > 0`, `uom`; optional `due_date`, `price`, `teardown`, `engine_sn`, `serial_num`, `packing`, header `order_date`, `received_date`, `warehouse`, `order_type`.
