# Contract: Mock control API (edge, loopback only)

Base: `http://127.0.0.1:${MOCK_EDGE_PORT:-4566}/__mock__`. JSON in/out. Never returns secret values.

| Method / path | Purpose | Result |
|---|---|---|
| `GET /health` | every service ready? | `{"ready": true, "services": {"moto": "up", "erp": "up", "sql": "up", "fn": "up"}}` |
| `POST /reset` | FR-010: all state to start, faults cleared | `{"reset": true}` |
| `GET /scenarios` | names | `["passes-review", ...]` |
| `POST /scenario/{name}` | load a scenario (implies reset) | `{"loaded": "name", "event": {...}}` |
| `POST /faults` | body `{"id": "...", "params": {...}, "remaining": n}` | `{"fault_id": "..."}`; unknown id -> 400 listing the catalogue |
| `DELETE /faults` | clear all (`/faults/{id}` one) | `{"cleared": n}` |
| `GET /calls?service=erp&since=seq` | call records | list per the data model |
| `POST /invoke` | send an event to the function (`mode`: `sync`/`async`, optional `timeout`) | `{"status": 200, "function_error": null, "payload": {...}, "duration_ms": n}` |
| `POST /invoke/api` | send an HTTP-style request through the API shim | proxy response |
| `POST /tick/sweep` | run one schedule tick now | invoke result |

Fault catalogue (initial): `idp.kms_denied`, `idp.lookup_denied`, `idp.not_found`, `erp.unreachable`, `erp.slow`, `erp.row_rejected`, `erp.session_expired`, `sql.unreachable`, `sql.login_refused`, `sql.routine_error`, `sql.slow`, `secrets.unavailable`, `lock.held` (another save holds the ERP write lock), `lambda.invoke_denied`.

Errors: 400 invalid input, 404 unknown scenario, 409 environment not ready. A request not from loopback is refused.
