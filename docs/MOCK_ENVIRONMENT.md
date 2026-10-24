# Production-like mock environment

Specs: `specs/008-production-like-mock-env/`. It runs the **built, unmodified function package** in the AWS Lambda Python 3.13
container image and points it at stand-ins that behave like production on the wire, so a deployment problem (a missing module,
a missing permission, a wrong address, an encryption key) shows up on your machine first. No AWS account and no network access to
the ERP are needed, and nothing leaves the machine.

It is a **preliminary** check. A pass is not proof of production behaviour; read [Fidelity gaps](#fidelity-gaps).

## What runs

| Service | Stands in for | How |
|---|---|---|
| `fn1`..`fn3` | the Lambda function | the AWS Lambda Python 3.13 image with the `sam build` output; settings, timeout and memory come from `src/template.yaml` |
| `edge` | AWS (DynamoDB, Secrets Manager, S3, KMS, CloudFormation, Lambda Invoke) | Moto behind a proxy that records every call, checks it against the function role's permissions **from the template**, applies faults, and answers Lambda `Invoke` (the function's own background start, the IDP lookup function) |
| `erp` | the SyteLine web service | a SOAP mock with a WSDL; the function's real client and zeep talk to it |
| `sqlserver-real` | the SyteLine database | a **real SQL Server 2022**, TLS required, a per-run certificate authority; the credentials secret carries the CA as `SQL_CA_PEM` |
| `driver` (optional) | the five-minute schedule | `--profile schedule`; ticks the sweep every 20 s |

Three function containers run side by side because the Lambda runtime emulator handles one invocation at a time, while the
real service starts more instances (the function invokes itself for its background work).

## Start, test, reset, stop

Prerequisites: Docker with Compose, the AWS SAM CLI, Python 3.9+, `pip install -r mock/requirements.txt -r src/requirements-dev.txt`,
and `IDP_REPO` pointing at the IDP accelerator checkout (the package needs its SDK, as in a deployment). On Apple silicon the
SQL Server image runs emulated and takes minutes to start.

```bash
export IDP_REPO=~/path/to/accelerated-intelligent-document-processing-on-aws
make -C mock up        # prepare, sam build (first time), start everything, wait until /__mock__/health says ready
make -C mock test      # up + MOCK_ENV=1 pytest tests/mock_env   (one run at a time)
make -C mock reset     # back to the starting state in seconds (data, locks, faults)
make -C mock down      # stop and remove
make -C mock logs
```

Without `MOCK_ENV=1` the `tests/mock_env` tests are skipped. With it and no environment running they stop with
"mock environment not running: start it with `make -C mock up`" and report no passes or failures.

By hand (control API on `127.0.0.1:4566`):

```bash
curl -X POST localhost:4566/__mock__/scenario/passes-review      # returns the event to send
curl -X POST localhost:4566/__mock__/invoke -d '{"event": {...}}'
curl localhost:4566/__mock__/calls?service=erp                    # what the ERP mock received
```

## Scenarios

All data is synthetic (nothing from production, sanitized or not). Regenerate with `python mock/scenarios/generate.py`.

| Scenario | Starting state | Expected |
|---|---|---|
| `passes-review` | every field above its confidence threshold | `save_accepted`, numbers reserved, order written to the mock ERP |
| `already-processed` | same event sent twice | second run `already_processed`, no second write |
| `needs-review` | `cust_po` below its threshold | `refused` / `DOCUMENT_NEEDS_REVIEW`, nothing written |
| `incomplete` | no PO number and no lines | `refused` / `DOCUMENT_INCOMPLETE` |
| `not-found` | the IDP has no record | `refused` / `DOCUMENT_NOT_READY` |
| `real-idp` | real-IDP mode only: your own finished document | a known outcome, reported (see Real IDP) |

## Faults

`POST /__mock__/faults {"id": ..., "params": {...}, "remaining": n}`; `DELETE /__mock__/faults` clears them all. Reset clears them too.

| Fault | Effect |
|---|---|
| `idp.kms_denied` | reading the IDP bucket is refused with the AWS `kms:Decrypt` sentence; `params.key_arn` names the key |
| `idp.lookup_denied` | invoking the IDP stack's lookup function is refused |
| `idp.not_found` | the IDP has no record of the loaded document |
| `erp.unreachable` | the web service answers HTTP 503 |
| `erp.slow` | answers after `params.delay_ms` (default 5000) |
| `erp.row_rejected` | `SaveDataSet` returns a row-level rejection inside HTTP 200; nothing stored |
| `erp.session_expired` | the next call faults as an expired session (the client logs in again once) |
| `sql.unreachable` | the database front closes every new connection |
| `sql.login_refused` | the function's login is disabled |
| `sql.routine_error` | the numbering routines raise an error |
| `sql.slow` | the numbering routines wait `params.delay_ms` (rounded up to whole seconds) |
| `secrets.unavailable` | Secrets Manager answers 503 |
| `lock.held` | another save holds the ERP-write lock (a live lease) |
| `lambda.invoke_denied` | the function may not invoke itself |

## Real IDP (opt-in)

By default the IDP is mocked with synthetic documents. To check how the function copes with **your real IDP's** output, turn on real-IDP
mode: only the function's IDP reads leave the mock; the ERP, the database, Secrets Manager, the jobs table and the function's self-start stay
mocked, so nothing is written to a real ERP.

```bash
aws sso login --profile idp-dev
make -C mock up MOCK_IDP=real MOCK_IDP_PROFILE=idp-dev \
     MOCK_IDP_STACK=us-ct-idp-stack MOCK_IDP_BUCKET=<output bucket> MOCK_IDP_TABLE=<tracking table> \
     MOCK_IDP_DOCUMENT=159112.pdf [MOCK_IDP_KMS_KEYS=<key arn,key arn>] [MOCK_IDP_REGION=us-gov-east-1] [MOCK_IDP_FIPS=false]
curl -X POST localhost:4566/__mock__/scenario/real-idp      # returns the event for MOCK_IDP_DOCUMENT; then POST it to /__mock__/invoke
make -C mock idp-creds                                       # credentials expired: re-export them, no restart
MOCK_ENV=1 MOCK_IDP=real pytest tests/mock_env/test_real_idp.py
```

How it works: the edge re-signs the function's IDP requests with temporary credentials exported from your profile
(`aws configure export-credentials`) into `mock/.idp/creds` (mode 0600, git-ignored, mounted into the edge container only; the function
never sees them) and sends them to the real service (FIPS endpoints by default for GovCloud). The template's permissions are still applied
first. **Only these calls leave the machine:** CloudFormation `DescribeStacks`/`ListStackResources`/`DescribeStackResource(s)` on the IDP
stack, `s3:GetObject` on its output bucket, DynamoDB `GetItem`/`Query`/`BatchGetItem` on its tracking table, and `lambda:InvokeFunction` on
`<stack>-LookupFunction-*` (it runs the IDP's own read-only status lookup). Any other IDP-bound call is refused by the edge and recorded
(`target: real, allowed: false`); no other real service is ever called.

- **The outcome is reported, not asserted.** The mock database and ERP start empty, so a real document usually ends `read_only` with
  `CUSTOMER_NOT_FOUND`. That shows the real document was read and parsed; to go further, add the matching customer to the mock data.
- **Decryption is the real thing:** the real key policy and your own KMS permissions apply, so the production `kms:Decrypt` failure
  reproduces for real, including key-policy denials the mock cannot model.
- **Credentials:** if they are missing or expired, `GET /__mock__/health` shows `"idp": "credentials expired at <time>"` (within 10 seconds)
  and the function's IDP reads fail with `IDP_UNAVAILABLE`; nothing falls back to the mock IDP. Mock scenarios answer 409 in this mode.
- **Not checked here:** the re-signing and the GovCloud endpoint names have only been tested against a stand-in service; the first run
  against real AWS may need a fix.

## Actions the function accepts

`health`, `add_note`, `get_notes`, `document_event` (the default), and the internal `work_upload`, `work_save`, `sweep`. The suite
checks this list against `src/lambda_function.py` (`mock/parity.py`), so an action added to the function fails the check until it is
listed here.

## Fidelity gaps

What the mock does differently from production, and what a pass therefore does **not** show:

- **Numbering routines.** `NextCoSp` / `NextWOSp` in the mock database are stand-ins that hand out increasing numbers. SyteLine's own
  routines (site rules, number formats, locking) are not reproduced.
- **Web-service shapes.** The WSDL and the SOAP shapes are the ones assumed in `src/syteline/client.py`, still unconfirmed against the real
  service. One detail is deliberate: `CallMethod` takes its parameters as a repeated element. With a wrapper type (`ArrayOfString`)
  zeep silently sends **no** parameters when handed a plain list; if the real WSDL has that shape, the function would call the numbering
  methods without parameters. Check this at discovery.
- **Unreachable ERP** is HTTP 503, not a refused TCP connection (so a write fault counts as "may have been sent").
- **Permissions.** The function role's statements from the template are enforced. Key policies, resource policies, permission boundaries,
  service control policies and condition keys are not: a denial caused only by the **key policy** of the IDP's KMS key does not reproduce.
- **FIPS endpoints.** `AWS_USE_FIPS_ENDPOINT` is set to `false`: botocore refuses FIPS together with a custom endpoint URL.
- **AWS services** are Moto's behaviour, not AWS's (limits, throttling, eventual consistency, error texts other than those listed).
  DynamoDB reserved words are enforced by Moto, which is how the `ttl` problem below was found.
- **Schedule** is compressed (20 s by default instead of 5 minutes) and off unless the `schedule` profile is used.
- **Network.** Security groups, routes, VPC endpoints and DNS split-horizon are not modelled; the compose network only guarantees the
  function cannot reach anything outside.
- **Runtime.** Python 3.13 and x86_64 as in the template; the emulator enforces the timeout but not memory beyond the container limit.
- **Live writes** are ON inside the mock (`SYTELINE_LIVE_WRITES=true`) so a save can be exercised. They can only reach the mock ERP and
  database: `make -C mock up` refuses to prepare, and the suite refuses to run, if the function's ERP or AWS address is not a mock address.
- **Real-IDP mode** is the one place anything leaves the machine, read-only and only to the IDP (see Real IDP); the no-egress guarantee of the function container still holds, because only the edge has a route out.
- **Not covered at all:** the IDP's own processing, the state machine and the EventBridge rule (the event is sent by the suite).

## Findings made while building this

- **DynamoDB reserved word.** `jobs.py` and `reservations.py` wrote `ttl = :ttl` in an update expression. `TTL` is a DynamoDB reserved word,
  so the update that ends a job or reservation was refused as soon as it ran against DynamoDB (Moto, like DynamoDB, rejects it).
  Fixed by aliasing it (`#ttl`); the unit-test fake now rejects unaliased reserved words so this cannot return unnoticed.
- **DynamoDB transactions** need the underlying actions (`PutItem`, `UpdateItem`, `DeleteItem`, `ConditionCheckItem`), not an action
  named after the transaction; the edge applies them item by item as AWS does.

## Troubleshooting

- `ERROR: set IDP_REPO ...` - export the checkout path before the first `up` (the build copies its SDK into the package).
- Not ready after 420 s - `make -C mock logs`; the health output names the service that is not `up` (SQL Server is the slow one).
  Raise the wait with `make -C mock up WAIT=900`.
- `refusing to prepare: these settings do not point at a mock service` - a parameter in `mock/render_env.py` names a real address.
- Port 4566 busy - `MOCK_EDGE_PORT=4570 make -C mock up` (and `MOCK_EDGE_URL=http://127.0.0.1:4570` for the tests).
- A test left a fault on - `make -C mock reset` clears it.
