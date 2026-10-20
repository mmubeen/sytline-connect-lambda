# Data Model: SyteLine ERP Integration

## Upload (DynamoDB, `pk = UPLOAD#<upload_id>`)
| Field | Type | Notes |
|---|---|---|
| upload_id | string (UUID) | the tracking ID returned to the caller; distinct from a SyteLine production job number |
| request_ref | string | 1–128 chars, caller supplied, unique |
| payload_hash | string | SHA-256 of the canonical request, detects ref reuse with a different body |
| request | map | the confirmed order as accepted (retained even if ERP/DB is down, FR-015) |
| uploaded_by | string | required, the confirming operator |
| caller_identity | string | IAM principal ARN from the API request context |
| state | enum | `accepted`, `running`, `succeeded`, `failed`, `needs_reconciliation` |
| failure | map | `{code, message, step}` when `failed` / `needs_reconciliation` |
| customer_match | map | `{cust_num, cust_seq}` once resolved |
| erp_records | map | `{co_num, lines:[{line, job}], possibly_written:[...]}` |
| attempts | number | number of worker runs |
| created_at / updated_at / ttl | number | epoch seconds; ttl 90 days after terminal state |

### State machine
```
accepted ──worker claims (conditional)──► running
running ──all rows verified by read-back──► succeeded
running ──definite failure (nothing written)──► failed   (retriable by a new submission with the same request_ref only after review)
running ──uncertain after an insert / stale running──► needs_reconciliation
accepted ──sweeper re-drives──► accepted (no write can have happened)
```
`needs_reconciliation` is terminal for automation: only a person, through an explicit operator action outside this service, closes it (`succeeded`/`failed`) or authorizes a new submission. No transition replays an upload automatically.

## RequestIndex (`pk = REF#<request_ref>`)
`{request_ref, upload_id, payload_hash}` written atomically with the upload (TransactWrite, condition `attribute_not_exists`).

## ConfirmedOrderRequest (API body)
- `request_ref` (required, 1–128)
- `confirmed` (required, must be `true`)
- `uploaded_by` (required, non-empty)
- `customer` (required object): `name` (required), `address1`, `city`, `state`, `postal_code` (optional but all used for matching if present). **Never a customer number.**
- `order_date` (default today, `YYYY-MM-DD`)
- `lines[]` (≥1): `item` (required), `quantity` (> 0), `uom` (required), `due_date`, `price` (≥ 0), `teardown` (optional string, written to `UserDefinedFields`)
- `warehouse`, `order_type` (default `SYTELINE_DEFAULT_WHSE` / `SYTELINE_DEFAULT_ORDTYPE`)

Validation reports all problems together (FR-002).

## ERP records (owned by SyteLine)
| Entity | Path | Key |
|---|---|---|
| Customer (read only) | Web service: customer-address collection (name confirmed by discovery), bill-to row | cust_num, cust_seq |
| Customer Order | Web service IDO `SLCos` (required CoNum, OrderDate, Stat) | CoNum from method `NextCoSp` |
| CO line | Web service IDO `CCT_Coitems` (+ teardown, see research open item 4) | CoNum + CoLine |
| Job (one per line) | Web service IDO `CCTJobs` (required Job, OrdType, Whse) | Job from method `NextWOSp` |
| Notes | Web service: method `CreateSpecificNoteSp` (write); collections `SpecificNotes` and `ObjectNotes` (read) | SpecificNoteToken; `ObjectNotes.RefRowPointer` → record `RowPointer` |

Relationships: CO 1—n lines; line 1—1 job; customer 1—n COs.
