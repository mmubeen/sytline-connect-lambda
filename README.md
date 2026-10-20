# sytline-connect-lambda

Integration with the SyteLine ERP from AWS GovCloud, keeping SyteLine on the plant network. Design
background: `docs/Instruction Sheet AWS GovCloud API Gateway to On-Premises Sy.docx`; requirements and
design: [`specs/001-syteline-erp-integration/`](specs/001-syteline-erp-integration/).

## Pieces

| Piece | Purpose |
|---|---|
| `POST /confirmed-orders` (`scr/handlers/intake.py`) | Validates an operator-confirmed order, durably records it, returns a job ID (HTTP 202) |
| `GET /upload-jobs/{id}` (`scr/handlers/status.py`) | State: `accepted`, `running`, `succeeded`, `failed`, `needs_reconciliation` |
| Worker (`scr/handlers/worker.py`, VPC) | Runs the upload: resolve customer (SQL `custaddr`) -> `NextCoSp` (SQL) -> `SLCos` / `CCT_Coitems` / `CCTJobs` (SOAP IDO) -> read-back |
| Sweeper (`scr/handlers/sweeper.py`) | Every 5 min: re-drives `accepted` jobs; parks stale `running` jobs as `needs_reconciliation` |
| Direct invoke (`scr/lambda_function.py`) | `health` (connectivity check), `add_note`, `get_notes` |

API shapes: [`contracts/api.md`](specs/001-syteline-erp-integration/contracts/api.md) and
[`contracts/lambda-actions.md`](specs/001-syteline-erp-integration/contracts/lambda-actions.md).
API: private REST API, `AWS_IAM` auth, reachable only through the approved `execute-api` VPC endpoint.

## Safety rules
- **Live writes are OFF by default** (`SYTELINE_LIVE_WRITES=false`). There is no test ERP. With the flag off
  the worker stops after the read-only customer match, before allocating any numbers. Turn it on only
  for a test the ERP owner approved, with a reconciliation and cleanup plan.
- **No automatic replay or delete.** Anything uncertain after a write may have happened becomes
  `needs_reconciliation`; see [`docs/reconciliation-runbook.md`](docs/reconciliation-runbook.md).
- Customer numbers are never accepted from callers; they come from the SyteLine `custaddr` match.
- Passwords come from Secrets Manager and are never logged or returned.

## Config
| Variable | Notes |
|---|---|
| `SYTELINE_WSDL` | required (HTTP, port 80 as documented) |
| `SYTELINE_USER`, `SYTELINE_PASS` | required (or supply via secret) |
| `SYTELINE_CONFIG` | default `CCT` |
| `SYTELINE_SECRET_ARN` | optional Secrets Manager JSON overriding env vars, e.g. `SYTELINE_PASS`, `SYTELINE_SQL_USER`, `SYTELINE_SQL_PASS` |
| `SYTELINE_SQL_HOST`, `SYTELINE_SQL_PORT`, `SYTELINE_SQL_DB`, `SYTELINE_SQL_USER`, `SYTELINE_SQL_PASS` | SQL Server. **No default port**: verify the listener with Chromalloy IT |
| `SYTELINE_LIVE_WRITES` | default `false` |
| `SYTELINE_DEFAULT_WHSE`, `SYTELINE_DEFAULT_ORDTYPE` | used when an order omits `warehouse` / `order_type` |
| `JOBS_TABLE`, `WORKER_FUNCTION_NAME` | DynamoDB table; worker invoked by intake and sweeper |
| `SYTELINE_VERIFY_TLS` | default `false` (SOAP is HTTP today) |
| `SYTELINE_TIMEOUT` | default `10` s |
| `SYTELINE_DRY_RUN` | default `false`; or send event `{"dry_run": true}` |

## Health check (`action: health`)
Stages `dns`, `tcp`, `wsdl`, `token`, stopping at the first failure. Event `{"check_sql": true}` adds a
final `sql` stage (TCP connect only). **Dry run** (`{"dry_run": true}`) needs only `SYTELINE_WSDL`: no
credentials, no SQL, no login. Stages `load`, `dns`, `tcp`, `wsdl`; the `wsdl` stage is a plain GET where
HTTP 401/403 passes.

## Tests
    pip install -r scr/requirements.txt -r scr/requirements-dev.txt && pytest
All tests use fakes and never touch the ERP. `tests/integration/` is skipped by default:
`SYTELINE_READONLY_CHECKS=1` runs read-only network/SQL checks from the worker network; the live tests
additionally need `SYTELINE_LIVE=1` **and** `LIVE_APPROVAL_REF` (the ERP owner's approval).

## Deploy (SAM)
`cd scr && sam build --use-container && sam deploy --guided`
Parameters include the SQL host/port/database, the API's VPC endpoint ID, and the worker's private
subnets and security groups (they need a route to the plant over the VPN, and outbound HTTP port 80
and the verified SQL port). Console deployment of the direct-invoke function: [`DEPLOY_PORTAL.md`](DEPLOY_PORTAL.md).

## Invoke (direct actions)
`aws lambda invoke --function-name <name> --payload '{"action":"health"}' --cli-binary-format raw-in-base64-out out.json && cat out.json`
(omit `--payload` for the default `health` action).
