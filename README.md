# sytline-connect-lambda

Integration with the SyteLine ERP from AWS GovCloud, keeping SyteLine on the plant network. Design
background: `docs/Instruction Sheet AWS GovCloud API Gateway to On-Premises Sy.docx`; requirements and
design: [`specs/001-syteline-erp-integration/`](specs/001-syteline-erp-integration/).

## Pieces

**One Lambda function** (`src/lambda_function.py`, in an existing VPC) does everything; the request decides what runs.

| What | How it is reached | Purpose |
|---|---|---|
| Order intake (`src/handlers/intake.py`) | `POST /confirmed-orders` | Validates an operator-confirmed order, durably records it, returns an upload ID (HTTP 202) |
| Upload status (`src/handlers/status.py`) | `GET /upload-jobs/{id}` | State: `accepted`, `running`, `succeeded`, `failed`, `needs_reconciliation` |
| Job-number reservation and save (`reserve.py`, `save_intake.py`, `reservation_status.py`) | `POST /job-reservations`, `POST /job-reservations/{id}/save`, `GET /job-reservations/{id}` | See [`docs/RESERVATIONS.md`](docs/RESERVATIONS.md) |
| Background work (`src/handlers/worker.py`) | `{"action": "work_upload"}` / `work_save` (the function starts these on itself) | Runs the upload or the save through the web service only, one at a time under an ERP-write lock |
| Cleanup (`src/handlers/sweeper.py`) | `{"action": "sweep"}` every 5 min | Re-drives `accepted` jobs; parks stale `running` jobs as `needs_reconciliation` |
| Direct actions | `{"action": "health" / "add_note" / "get_notes"}` | Connectivity check, notes |
| IDP event intake (`src/handlers/event_intake.py`) | `{"action": "document_event"}`, the **default** when no `action` is given | See [`docs/EVENT_INTAKE.md`](docs/EVENT_INTAKE.md) |

API shapes: [`contracts/api.md`](specs/001-syteline-erp-integration/contracts/api.md) and
[`contracts/lambda-actions.md`](specs/001-syteline-erp-integration/contracts/lambda-actions.md).
API: private REST API, `AWS_IAM` auth, reachable only through the approved `execute-api` VPC endpoint.

## Safety rules
- **ERP records are saved through the SyteLine web service; the SyteLine database is used only for three calls the web service has no endpoint for** (reserving CO/job numbers, finding the customer, reading the customer's default terms and ship-via), and it never inserts, updates or deletes a row directly. See [`docs/DATABASE.md`](docs/DATABASE.md).
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
| `JOBS_TABLE` | DynamoDB table (set by the template); the function starts its own background work (`WORKER_FUNCTION_NAME` defaults to its own name) |
| `SYTELINE_VERIFY_TLS` | default `false` (SOAP is HTTP today) |
| `SYTELINE_TIMEOUT` | default `10` s |
| `SYTELINE_DRY_RUN` | default `false`; or send event `{"dry_run": true}` |

## Health check (`action: health`)
Stages `dns`, `tcp`, `wsdl`, `token`, stopping at the first failure. **Dry run** (`{"dry_run": true}`)
needs only `SYTELINE_WSDL`: no credentials, no login. Stages `load`, `dns`, `tcp`, `wsdl`; the `wsdl` stage is a plain GET where
HTTP 401/403 passes.

## Tests
    pip install -r src/requirements.txt -r src/requirements-dev.txt && pytest
All tests use fakes and never touch the ERP. `tests/integration/` is skipped by default:
`SYTELINE_READONLY_CHECKS=1` runs read-only network and web-service checks from the worker network; the live tests
additionally need `SYTELINE_LIVE=1` **and** `LIVE_APPROVAL_REF` (the ERP owner's approval).

## Deploy (SAM)
One stack (`sytline-connect`) in **us-gov-east-1**, one configuration (`default` in `src/samconfig.toml`).

    pip install aws-sam-cli            # or the official installer
    cd src
    cp deploy.env.example deploy.env   # fill in; git-ignored, holds no passwords
    ./deploy.sh --profile NAME --validate   # lint the template
    ./deploy.sh --profile NAME --build      # sam build (no Docker, no local Python 3.13 needed)
    ./deploy.sh --profile NAME              # build + deploy; confirms the change set interactively

`--profile NAME` selects the AWS credentials profile for every step (or set `AWS_PROFILE_NAME` in `deploy.env`; omit both to use
the default credential chain).

- **Build:** `src/Makefile` (selected by `Metadata: BuildMethod: makefile`) installs the dependencies as
  Linux x86_64 / CPython 3.13 wheels into the `sytline-connect-deps` layer and copies the code into each function.
- **Network:** the deployment creates **no** network resource. Give it an existing VPC, subnets (two Availability Zones recommended) and
  security group(s) plus an existing `execute-api` endpoint (`WORKER_VPC_ID`, `WORKER_SUBNET_IDS`, `WORKER_SG_IDS`, `API_VPCE_ID`, all required). The
  network must provide a route to the ERP and the database and a private path to Secrets Manager, DynamoDB and the IDP; see
  [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) section 3a. Coming from an earlier shape: [`docs/MIGRATION.md`](docs/MIGRATION.md).
- **Parameters:** site-specific values (web-service WSDL and user, secret ARN, the four network values) come from `deploy.env`; `deploy.sh` refuses to run if any is missing or malformed.
- **Secret:** create the Secrets Manager secret (JSON with `SYTELINE_PASS`) in us-gov-east-1 first and put its ARN in `deploy.env`.
- **Live writes stay off.** `LIVE_WRITES=false` is the default. `deploy.sh` will not deploy with `LIVE_WRITES=true` unless
  `LIVE_APPROVAL_REF` (the ERP owner's written approval) is also set; redeploy with it off afterwards.
- **Outputs:** API id, the function name and ARN, job table name, the attached VPC. The API is private: call it from the
  approved VPC endpoint with IAM (SigV4) credentials.
- Full step-by-step guide (prerequisites, verification, enabling live writes, troubleshooting, teardown): [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Job-number reservations
A two-stage alternative to the one-step order upload: `POST /job-reservations` reserves the CO and job numbers from SyteLine (writes no
order, line or job), then `POST /job-reservations/{id}/save` saves the order information using those numbers, following the PO OCR
demo's flow. `GET /job-reservations/{id}` explains what happened. See [`docs/RESERVATIONS.md`](docs/RESERVATIONS.md).
The same workflow can be started automatically by the IDP's finished-document event (`IDP_STATE_MACHINE_ARN`): see [`docs/EVENT_INTAKE.md`](docs/EVENT_INTAKE.md).

## Testing the deployment
A step-by-step test sequence, from credential-free checks to an approved live-write window: [`docs/TESTING.md`](docs/TESTING.md).

## Invoke (direct actions)
`aws lambda invoke --function-name <name> --payload '{"action":"health"}' --cli-binary-format raw-in-base64-out out.json && cat out.json`
The health check **must** be asked for with `"action":"health"`: a payload without an `action` is treated as an IDP finished-document event (the default action, `document_event`) and a payload that is not such an event is refused with a message that says so ([`docs/EVENT_INTAKE.md`](docs/EVENT_INTAKE.md)).

## Unconfirmed web-service names
`src/syteline/idos.py` is the single place for the collection, method and property names, and
`src/syteline/client.py` for the SOAP operation names and payload shapes. They are best guesses, verified only
against fakes, until the read-only discovery task (T013) is run on the plant network. Do not enable live
writes before then.
