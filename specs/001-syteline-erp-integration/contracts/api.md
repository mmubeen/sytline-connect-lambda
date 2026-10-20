# Contract: private REST API

API Gateway REST API, endpoint type **Private**, reachable only through the approved `execute-api` VPC endpoint (resource policy on `aws:SourceVpce`). Authorization: `AWS_IAM` on every route. Lambda proxy integration. All bodies JSON.

## POST /confirmed-orders
Accepts an operator-confirmed order, durably records it, starts the upload, returns a upload ID.

Request:
```json
{"request_ref":"ocr-2026-000123","confirmed":true,"uploaded_by":"jdoe",
 "customer":{"name":"ACME AEROSPACE","address1":"1 MAIN ST","city":"PHOENIX","state":"AZ","postal_code":"85001"},
 "order_date":"2026-10-20",
 "lines":[{"item":"ABC-100","quantity":5,"uom":"EA","due_date":"2026-11-01","teardown":"..."}]}
```
Responses:
| Status | Body |
|---|---|
| 202 | `{"upload_id":"…","state":"accepted","duplicate":false}` |
| 200 | `{"upload_id":"…","state":"<current>","duplicate":true}` — same `request_ref` and same payload |
| 400 | `{"errors":[{"code":"VALIDATION","field":"…","message":"…"}]}` — nothing recorded |
| 409 | `{"errors":[{"code":"REQUEST_REF_CONFLICT",…}]}` — same `request_ref`, different payload |
| 403 | API Gateway/IAM denial |

## GET /upload-jobs/{id}
| Status | Body |
|---|---|
| 200 | `{"upload_id","request_ref","uploaded_by","state","co_num?","jobs?":[{"line","job"}],"failure?":{"code","message","step"},"possibly_written?":[{"kind","key"}],"created_at","updated_at"}` |
| 404 | `{"errors":[{"code":"NOT_FOUND"}]}` |

States: `accepted`, `running`, `succeeded`, `failed`, `needs_reconciliation` (see data-model.md). `co_num`/`jobs` are present only on `succeeded`. `possibly_written` is present on `needs_reconciliation`.

Failure codes: `CUSTOMER_NOT_FOUND`, `CUSTOMER_AMBIGUOUS`, `LIVE_WRITES_DISABLED`, `ERP_UNREACHABLE`, `ERP_REJECTED` (ERP text included), `READBACK_MISMATCH`, `UNCERTAIN_OUTCOME`, `STALE_RUNNING`.

Only `accepted` jobs are ever re-driven automatically. `needs_reconciliation` jobs are never replayed.
