# Contract: event intake

## Invocation
`EventIntakeFunction` is started by EventBridge (when `IdpStateMachineArn` is set) or invoked directly with the event as payload (an object or JSON text). Asynchronous invocation has **no retries** (R4); the response is discarded by EventBridge and is the direct-invoke result.

## Template parameter (plant only)
| Parameter | Default | Meaning |
|---|---|---|
| `IdpStateMachineArn` | `""` | ARN of the IDP document-processing state machine. Empty: no rule, nothing starts automatically. Given: a rule for its `SUCCEEDED` status-change events starts the function. Same account and region as this stack. |

Environment of the function: as `ReserveFunction`, plus `WORKER_FUNCTION_NAME`, `SYTELINE_DEFAULT_WHSE`, `SYTELINE_DEFAULT_ORDTYPE`.

## Response
```json
{"outcome": "save_accepted", "document_id": "159112.pdf", "reservation_id": "…", "co_num": "…", "jobs": [{"line": 1, "job": "…"}],
 "summary": {"document_id": "159112.pdf", "state": "COMPLETED", "class": "purchase_order", "pages": 3, "issues": 1,
             "fields": [{"path": "po_number", "value": "…", "confidence": 0.97, "meets_threshold": true}],
             "below_threshold": ["line_items[0].material_code"]}}
```
`outcome` is one of:
| outcome | when | writes |
|---|---|---|
| `ignored` (+ `reason`) | status not `SUCCEEDED`, other source or detail type | none; nothing contacted |
| `refused` (+ `errors[]`) | `EVENT_INVALID`, `DOCUMENT_NOT_FOUND`, `DOCUMENT_NOT_READY`, `DOCUMENT_INCOMPLETE`, `DOCUMENT_NEEDS_REVIEW` (+ `fields`), `IDP_UNAVAILABLE`, `DEFAULTS_NOT_CONFIGURED` | none; summary included when the result could be read |
| `read_only` (+ `reason`) | usable result but `LIVE_WRITES_DISABLED`, or `CUSTOMER_NOT_FOUND`/`AMBIGUOUS`/`LOW_CONFIDENCE`, `ERP_UNREACHABLE` before any number | none; summary included |
| `reserved` / `save_accepted` | numbers reserved / save handed to the worker | numbers, then the order (by the worker) |
| `already_processed` | the document has a reservation or an `EVTDOC#` record | none; the first outcome is returned (`first_outcome`) |
| `failed` (+ `errors[]`) | reservation failed or uncertain | numbers may be consumed; never retried |

`summary` is omitted for `ignored` and for `EVENT_INVALID`; field `value` appears in the response only.

## Event rule (when `IdpStateMachineArn` is set)
```json
{"source": ["aws.states"], "detail-type": ["Step Functions Execution Status Change"],
 "detail": {"status": ["SUCCEEDED"], "stateMachineArn": ["<IdpStateMachineArn>"]}}
```

## Deploy settings
`deploy.env` (PLANT ONLY block): `IDP_STATE_MACHINE_ARN` -> `IdpStateMachineArn` (empty by default, always passed so an earlier value is cleared). The parity tests need no change (plant-only parameter).
