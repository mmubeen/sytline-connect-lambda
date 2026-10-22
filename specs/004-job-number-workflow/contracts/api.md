# Contract: job reservation routes (private REST API)

Added to the existing private API (same endpoint restriction, `AWS_IAM` on every route, Lambda proxy, JSON bodies). Error body shape and codes as in [feature 001's api.md](../../001-syteline-erp-integration/contracts/api.md). Available in the VPC deployment only.

## POST /job-reservations
Reserves one CO number and one job number per line of the IDP document `document_id`. Synchronous. Writes no order, line or job. The customer, PO number and lines are read from the document's inference result (never sent, never stored).

Request:
```json
{"request_ref":"ocr-2026-000123","confirmed":true,"uploaded_by":"jdoe","document_id":"batch-20261021/PO-77812.pdf"}
```
| Status | Body |
|---|---|
| 201 | `{"reservation_id","state":"reserved","duplicate":false,"document_id","line_count","co_num":"…","jobs":[{"line":1,"job":"…"},…],"customer_match":{"cust_num","cust_seq"}}` |
| 200 | same body with `"duplicate":true` — same `request_ref` and payload; no new numbers are reserved. If the earlier reservation failed, the body has `"state":"failed"`, `"failure"` and `"consumed_unused"` |
| 400 | `{"errors":[{"code":"VALIDATION","field","message"}]}` — nothing reserved (`customer`, `po_number`, `line_count`, `lines` are refused: they come from the document) |
| 409 | `REQUEST_REF_CONFLICT` — same ref, different payload |
| 422 | `CUSTOMER_NOT_FOUND` / `CUSTOMER_AMBIGUOUS` / `LIVE_WRITES_DISABLED` — nothing reserved |
| 502 | `ERP_UNREACHABLE` / `ERP_REJECTED` / `TIME_BUDGET_EXHAUSTED` — body includes `"reservation_id"`, `"state":"failed"` and, when some numbers were obtained, `"consumed_unused":[…]` |
| 503 | `STORE_UNAVAILABLE` — nothing reserved |

## POST /job-reservations/{id}/save
Names the same IDP document and starts the save with the reserved numbers. Asynchronous. The PO details are read from the document again when the save runs.

Request:
```json
{"confirmed":true,"uploaded_by":"jdoe","document_id":"batch-20261021/PO-77812.pdf",
 "order_date":"2026-10-20","warehouse":"MAIN","order_type":"J"}
```
| Status | Body |
|---|---|
| 202 | `{"reservation_id","state":"save_accepted"}` |
| 400 | `VALIDATION` — nothing written |
| 403 | `RESERVATION_FORBIDDEN` — other caller or other `uploaded_by` (also API Gateway/IAM denial) |
| 404 | `NOT_FOUND` |
| 409 | `RESERVATION_USED` (a save was already accepted; body carries the current `state`) / `RESERVATION_NOT_READY` (still reserving, or failed) |
| 422 | `RESERVATION_MISMATCH` — `document_id` differs from the reserved document; `DOCUMENT_CHANGED` — the document's customer, PO number or line count no longer match |
| 503 | `STORE_UNAVAILABLE` — nothing recorded; the reservation is still usable |

Nothing is written to the ERP in any non-202 case. Checks run in this order: 400, 404, 403, 409 (not ready / used), 422, then the claim (a lost race is also 409).

## GET /job-reservations/{id}
| Status | Body |
|---|---|
| 200 | `{"reservation_id","request_ref","document_id","uploaded_by","state","co_num","document_id","jobs":[{"line","job","usage":"reserved\|used\|consumed_unused\|unverified"}],"lines?":[{"line","job","co_line?","item?","status":"saved\|failed\|not_attempted\|unverified","warnings":[…],"error?"}],"failure?":{"code","message","step"},"possibly_written?":[{"kind","key"}],"created_at","updated_at"}` |
| 404 | `NOT_FOUND` |

States: `reserving`, `reserved`, `save_accepted`, `saving`, `saved`, `partly_saved`, `failed`, `needs_reconciliation` (see [data-model.md](../data-model.md)). `lines` appears once a save was accepted. `possibly_written` appears on `needs_reconciliation`.

New error codes: `TOO_MANY_LINES`, `RESERVATION_USED`, `RESERVATION_MISMATCH`, `RESERVATION_FORBIDDEN`, `RESERVATION_NOT_READY`. Existing codes are reused unchanged.

Only `save_accepted` reservations are ever re-driven automatically. `needs_reconciliation` and `partly_saved` are never replayed.


## Document source (revision 2026-10-21)

The PO details are read from the IDP result of `document_id` through the IDP SDK: the document must have finished processing, every field the save needs must be present, and every such field the IDP assessed must meet its confidence threshold. Reserve and save-intake refuse otherwise, and the worker checks again when it runs.

**Refusals** (reserve and save; nothing reserved, recorded or written):
| Status | Code | Meaning |
|---|---|---|
| 404 | `DOCUMENT_NOT_FOUND` | no such document in IDP |
| 409 | `DOCUMENT_NOT_READY` | not finished processing; the body's `state` names it (for example `HITL_IN_PROGRESS`) |
| 422 | `DOCUMENT_INCOMPLETE` | a needed field is missing or unreadable, or the class is not the expected one; `fields` lists them |
| 422 | `DOCUMENT_NEEDS_REVIEW` | needed fields below their confidence threshold; `fields` lists them; review them in IDP first |
| 422 | `TOO_MANY_LINES` | more than 25 lines in the document |
| 422 | `DOCUMENT_CHANGED` | save only: customer, PO number or line count differ from what was reserved |
| 502 | `IDP_UNAVAILABLE` | the IDP source could not be read |

When the save runs and the document is no longer usable, the reservation ends `failed` with the document code and nothing written. If the IDP source is merely unavailable at that moment, the save goes back to `save_accepted` and the sweeper starts it again later (up to 5 attempts, then `failed` with `IDP_UNAVAILABLE`).
