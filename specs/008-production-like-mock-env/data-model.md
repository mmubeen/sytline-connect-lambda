# Data Model: Production-Like Mock Environment

## Scenario (`mock/scenarios/<name>/`)
| File | Content |
|------|---------|
| `scenario.json` | `name`, `description`, `event` (Step Functions status-change event to send), `expect` (outcome, error codes) |
| `idp.json` | document id, class, fields, confidence per field, status, pages |
| `erp.json` | IDO collections -> rows (customers, items, existing orders) |
| `sql.sql` | rows for `custaddr` and `co`, starting number for each sequence |
| `secrets.json` | non-sensitive settings; passwords are generated per run |

Rules: synthetic values only (FR-015); a scenario never contains a real customer, part or document; names are unique.

## Fault (`/__mock__/faults`)
| Field | Meaning |
|-------|---------|
| `id` | catalogue name, e.g. `idp.kms_denied`, `erp.unreachable`, `sql.login_refused` |
| `target` | service the fault applies to |
| `params` | optional, e.g. `{"key_arn": "..."}`, `{"delay_ms": 20000}` |
| `remaining` | number of calls it applies to, or `null` until cleared |
State: `active -> cleared` (explicitly, on exhaustion, or by reset).

## Call record
`{seq, ts, service, operation, caller, action, resource, allowed, fault, request_summary, status}`. Bodies are stored redacted (no passwords, tokens, secret values). Ordered by `seq`; cleared by reset.

## Fidelity gap
`{area, what production does, what the mock does, effect on a pass}` - listed in `docs/MOCK_ENVIRONMENT.md` and asserted present by `test_parity.py`.

## Mock environment state
`idle -> starting -> ready -> (scenario loaded) -> running tests -> reset -> ready`; `ready` requires every service health check to pass.
