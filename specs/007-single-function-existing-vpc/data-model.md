# Data Model: One Function, Always in an Existing VPC

The jobs table and every existing item kind (`UPLOAD#`, `REF#`, `RESERVATION#`, `RESREF#`, `DOCRES#`, `EVTDOC#`) are unchanged. One item kind is added; the table gets protection policies.

## LOCK#erp-writes (new): the ERP-write lease
| Field | Rule |
|---|---|
| `pk` | exactly `LOCK#erp-writes` (one item for the whole deployment) |
| `holder` | the invocation's request ID (string, 1 to 128 characters) |
| `action` | `work_upload` or `work_save` (informational) |
| `acquired_at` | epoch seconds |
| `expires_at` | epoch seconds = `acquired_at` + 330 (function timeout 300 + 30) |
| `ttl` | `expires_at` + 3600, so DynamoDB removes a long-dead lock |

State transitions: absent or `expires_at` <= now -> **held** by `holder` (conditional put: `attribute_not_exists(pk) OR expires_at <= :now`); held -> **released** (delete, conditional on `holder = :me`). A holder that finds the item no longer its own (it expired and was taken over) must not release it and must not claim further jobs. No PO or field data is stored.

## JobsTable (resource)
Logical ID `JobsTable`, hash key `pk` (S), TTL attribute `ttl`, point-in-time recovery on, `DeletionPolicy: Retain`, `UpdateReplacePolicy: Retain` (new). The key schema is never changed by this feature (a change would replace the table).

## Invocation (input, not stored)
One of: an API Gateway REST proxy event (`httpMethod`, `resource`, `pathParameters`, `body`, `requestContext`), a payload with an `action` (`health`, `add_note`, `get_notes`, `document_event`, `work_upload` + `upload_id`, `work_save` + `reservation_id`, `sweep`), or an IDP status change event with no `action`. See contracts/dispatch.md.

## Removed
No data is removed. Settings that disappear: see contracts/deployment.md.
