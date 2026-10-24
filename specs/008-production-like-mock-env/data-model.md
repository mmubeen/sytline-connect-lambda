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

## Real-IDP configuration (`mock/.idp/mode.json`, git-ignored)
| Field | Meaning |
|-------|---------|
| `mode` | `mock` (default) or `real` |
| `profile` | AWS profile the credentials were exported from (a name, not a secret) |
| `stack`, `bucket`, `table`, `kms_keys`, `region`, `account` | the real IDP identifiers used to route requests and render the permissions |
| `document` | the document id to process (required in real mode) |
| `fips` | use FIPS endpoints (default true for `aws-us-gov`) |

## Real-IDP credentials (`mock/.idp/creds`, mode 0600)
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_CREDENTIAL_EXPIRATION`. Mounted read-only into the edge only; never in an
environment variable, a call record, a log line or a response. State: `missing -> valid -> expired -> (refreshed) valid`.

## Call record (extended)
Adds `target`: `mock` or `real`. A `real` record carries only `service`, `operation`, `action`, `resource`, `allowed`, `status`.
