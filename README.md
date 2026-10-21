# sytline-connect-lambda

Integration with the SyteLine ERP from AWS GovCloud, keeping SyteLine on the plant network. Design
background: `docs/Instruction Sheet AWS GovCloud API Gateway to On-Premises Sy.docx`; requirements and
design: [`specs/001-syteline-erp-integration/`](specs/001-syteline-erp-integration/).

## Pieces

| Piece | Purpose |
|---|---|
| `POST /confirmed-orders` (`scr/handlers/intake.py`) | Validates an operator-confirmed order, durably records it, returns an upload ID (HTTP 202) |
| `GET /upload-jobs/{id}` (`scr/handlers/status.py`) | State: `accepted`, `running`, `succeeded`, `failed`, `needs_reconciliation` |
| Worker (`scr/handlers/worker.py`, VPC) | Runs the upload through the web service only: look up the customer (read) -> `NextCoSp` (method call) -> `SLCos` / `CCT_Coitems` / `CCTJobs` (inserts) -> read-back |
| Sweeper (`scr/handlers/sweeper.py`) | Every 5 min: re-drives `accepted` jobs; parks stale `running` jobs as `needs_reconciliation` |
| Direct invoke (`scr/lambda_function.py`) | `health` (connectivity check), `add_note`, `get_notes` |

API shapes: [`contracts/api.md`](specs/001-syteline-erp-integration/contracts/api.md) and
[`contracts/lambda-actions.md`](specs/001-syteline-erp-integration/contracts/lambda-actions.md).
API: private REST API, `AWS_IAM` auth, reachable only through the approved `execute-api` VPC endpoint.

## Safety rules
- **All ERP access is through the SyteLine web service** (FR-018): no SQL connection, driver or database credential exists.
- **Live writes are OFF by default** (`SYTELINE_LIVE_WRITES=false`). There is no test ERP. With the flag off
  the worker stops after the read-only customer match, before allocating any numbers. Turn it on only
  for a test the ERP owner approved, with a reconciliation and cleanup plan.
- **No automatic replay or delete.** Anything uncertain after a write may have happened becomes
  `needs_reconciliation`; see [`docs/reconciliation-runbook.md`](docs/reconciliation-runbook.md).
- Customer numbers are never accepted from callers; they come from the SyteLine customer-address lookup.
- Passwords come from Secrets Manager and are never logged or returned.

## Config
| Variable | Notes |
|---|---|
| `SYTELINE_WSDL` | required (HTTP, port 80 as documented) |
| `SYTELINE_USER`, `SYTELINE_PASS` | required (or supply via secret) |
| `SYTELINE_CONFIG` | default `CCT` |
| `SYTELINE_SECRET_ARN` | optional Secrets Manager JSON overriding env vars, e.g. `SYTELINE_PASS` |
| `SYTELINE_LIVE_WRITES` | default `false` |
| `SYTELINE_DEFAULT_WHSE`, `SYTELINE_DEFAULT_ORDTYPE` | used when an order omits `warehouse` / `order_type` |
| `JOBS_TABLE`, `WORKER_FUNCTION_NAME` | DynamoDB table; worker invoked by intake and sweeper |
| `SYTELINE_VERIFY_TLS` | default `false` (SOAP is HTTP today) |
| `SYTELINE_TIMEOUT` | default `10` s |
| `SYTELINE_DRY_RUN` | default `false`; or send event `{"dry_run": true}` |

## Health check (`action: health`)
Stages `dns`, `tcp`, `wsdl`, `token`, stopping at the first failure. **Dry run** (`{"dry_run": true}`)
needs only `SYTELINE_WSDL`: no credentials, no login. Stages `load`, `dns`, `tcp`, `wsdl`; the `wsdl` stage is a plain GET where
HTTP 401/403 passes.

## Tests
    pip install -r scr/requirements.txt -r scr/requirements-dev.txt && pytest
All tests use fakes and never touch the ERP. `tests/integration/` is skipped by default:
`SYTELINE_READONLY_CHECKS=1` runs read-only network and web-service checks from the worker network; the live tests
additionally need `SYTELINE_LIVE=1` **and** `LIVE_APPROVAL_REF` (the ERP owner's approval).

## Deploy (SAM)
One stack (`sytline-connect`) in **us-gov-east-1**, one configuration (`default` in `scr/samconfig.toml`).

    pip install aws-sam-cli            # or the official installer
    cd scr
    cp deploy.env.example deploy.env   # fill in; git-ignored, holds no passwords
    ./deploy.sh --profile NAME --validate   # lint the template
    ./deploy.sh --profile NAME --build      # sam build (no Docker, no local Python 3.13 needed)
    ./deploy.sh --profile NAME              # build + deploy; confirms the change set interactively

`--profile NAME` selects the AWS credentials profile for every step (or set `AWS_PROFILE_NAME` in `deploy.env`; omit both to use
the default credential chain).

- **Build:** `scr/Makefile` (selected by `Metadata: BuildMethod: makefile`) installs the dependencies as
  Linux x86_64 / CPython 3.13 wheels into the `sytline-connect-deps` layer and copies the code into each function.
- **Host network:** supply an existing one (all four of VPC, subnets, security groups, route tables), or leave all four empty and
  set `HOST_NETWORK_CIDR` and `PLANT_CIDR_1` to have the stack create a private one (two subnets, no route out, security groups,
  endpoints; optionally the API-caller endpoint). The plant connection (VPN, routes) is never created: the network team adds it
  from the stack outputs. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) section 3a.
- **Parameters:** site-specific values (web-service WSDL and user, secret ARN, the `execute-api` VPC endpoint,
  worker VPC / subnets / security groups / route tables) come from `deploy.env`; `deploy.sh` refuses to run if any is missing.
  The worker needs a route to the plant over the VPN and outbound HTTP port 80 to the web service. The template
  also creates DynamoDB and Secrets Manager VPC endpoints (`CREATE_VPC_ENDPOINTS=false` if the VPC already reaches them).
- **Secret:** create the Secrets Manager secret (JSON with `SYTELINE_PASS`) in us-gov-east-1 first and put its ARN in `deploy.env`.
- **Live writes stay off.** `LIVE_WRITES=false` is the default. `deploy.sh` will not deploy with `LIVE_WRITES=true` unless
  `LIVE_APPROVAL_REF` (the ERP owner's written approval) is also set; redeploy with it off afterwards.
- **Outputs:** API id, worker and direct-invoke function names, job table name. The API is private: call it from the
  approved VPC endpoint with IAM (SigV4) credentials.
- Full step-by-step guide (prerequisites, verification, enabling live writes, troubleshooting, teardown): [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).
- Console deployment of only the direct-invoke function: [`DEPLOY_PORTAL.md`](DEPLOY_PORTAL.md).

## Stand-alone version (no VPC)
`./deploy.sh --standalone` deploys only the connectivity check and the notes operations as a separate stack that **creates no network
resources**, callable only with IAM-permitted cloud credentials (no API gateway, no public address). By default it runs **outside any VPC**
(the ERP must then be reachable from the internet); with the three `ATTACH_*` settings it runs **inside an existing VPC, subnets and security
group** instead. It requires **HTTPS** to the ERP in both modes (plain HTTP only with an explicit override that sends credentials unencrypted), and cannot use ERP source-address
allow-listing. See [`docs/STANDALONE.md`](docs/STANDALONE.md).

## Invoke (direct actions)
`aws lambda invoke --function-name <name> --payload '{"action":"health"}' --cli-binary-format raw-in-base64-out out.json && cat out.json`
(omit `--payload` for the default `health` action).

## Unconfirmed web-service names
`scr/syteline/idos.py` is the single place for the collection, method and property names, and
`scr/syteline/client.py` for the SOAP operation names and payload shapes. They are best guesses, verified only
against fakes, until the read-only discovery task (T013) is run on the plant network. Do not enable live
writes before then.
