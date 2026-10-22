# Contract: job reservation routes (private REST API)

Added to the existing private API (same endpoint restriction, `AWS_IAM` on every route, Lambda proxy, JSON bodies). Error body shape and codes as in [feature 001's api.md](../../001-syteline-erp-integration/contracts/api.md). Available in the VPC deployment only.

## POST /job-reservations
Reserves one CO number and one job number per line. Synchronous. Writes no order, line or job.

Request:
```json
{"request_ref":"ocr-2026-000123","confirmed":true,"uploaded_by":"jdoe",
 "customer":{"name":"ACME AEROSPACE","address1":"1 MAIN ST","city":"PHOENIX","state":"AZ","postal_code":"85001"},
 "po_number":"PO-77812","line_count":3}
```
| Status | Body |
|---|---|
| 201 | `{"reservation_id","state":"reserved","duplicate":false,"co_num":"…","jobs":[{"line":1,"job":"…"},…],"customer_match":{"cust_num","cust_seq"}}` |
| 200 | same body with `"duplicate":true` — same `request_ref` and payload; no new numbers are reserved. If the earlier reservation failed, the body has `"state":"failed"`, `"failure"` and `"consumed_unused"` |
| 400 | `{"errors":[{"code":"VALIDATION"\|"TOO_MANY_LINES","field","message"}]}` — nothing reserved |
| 409 | `REQUEST_REF_CONFLICT` — same ref, different payload |
| 422 | `CUSTOMER_NOT_FOUND` / `CUSTOMER_AMBIGUOUS` / `LIVE_WRITES_DISABLED` — nothing reserved |
| 502 | `ERP_UNREACHABLE` / `ERP_REJECTED` / `TIME_BUDGET_EXHAUSTED` — body includes `"reservation_id"`, `"state":"failed"` and, when some numbers were obtained, `"consumed_unused":[…]` |
| 503 | `STORE_UNAVAILABLE` — nothing reserved |

## POST /job-reservations/{id}/save
Accepts the PO details and starts the save with the reserved numbers. Asynchronous.

Request:
```json
{"confirmed":true,"uploaded_by":"jdoe",
 "customer":{"name":"ACME AEROSPACE","address1":"1 MAIN ST","city":"PHOENIX","state":"AZ","postal_code":"85001"},
 "po_number":"PO-77812","order_date":"2026-10-20",
 "lines":[{"item":"ABC-100","quantity":5,"uom":"EA","due_date":"2026-11-01","teardown":"2026-10-25",
           "engine_sn":"E123","serial_num":"S456","packing":"BOX"}]}
```
| Status | Body |
|---|---|
| 202 | `{"reservation_id","state":"save_accepted"}` |
| 400 | `VALIDATION` — nothing written |
| 403 | `RESERVATION_FORBIDDEN` — other caller or other `uploaded_by` (also API Gateway/IAM denial) |
| 404 | `NOT_FOUND` |
| 409 | `RESERVATION_USED` (a save was already accepted; body carries the current `state`) / `RESERVATION_NOT_READY` (still reserving, or failed) |
| 422 | `RESERVATION_MISMATCH` — customer, PO number or line count differ from the reservation |
| 503 | `STORE_UNAVAILABLE` — nothing recorded; the reservation is still usable |

Nothing is written to the ERP in any non-202 case. Checks run in this order: 400, 404, 403, 409 (not ready / used), 422, then the claim (a lost race is also 409).

## GET /job-reservations/{id}
| Status | Body |
|---|---|
| 200 | `{"reservation_id","request_ref","po_number","uploaded_by","state","co_num","jobs":[{"line","job","usage":"reserved\|used\|consumed_unused\|unverified"}],"lines?":[{"line","job","co_line?","item?","status":"saved\|failed\|not_attempted\|unverified","warnings":[…],"error?"}],"failure?":{"code","message","step"},"possibly_written?":[{"kind","key"}],"created_at","updated_at"}` |
| 404 | `NOT_FOUND` |

States: `reserving`, `reserved`, `save_accepted`, `saving`, `saved`, `partly_saved`, `failed`, `needs_reconciliation` (see [data-model.md](../data-model.md)). `lines` appears once a save was accepted. `possibly_written` appears on `needs_reconciliation`.

New error codes: `TOO_MANY_LINES`, `RESERVATION_USED`, `RESERVATION_MISMATCH`, `RESERVATION_FORBIDDEN`, `RESERVATION_NOT_READY`. Existing codes are reused unchanged.

Only `save_accepted` reservations are ever re-driven automatically. `needs_reconciliation` and `partly_saved` are never replayed.
