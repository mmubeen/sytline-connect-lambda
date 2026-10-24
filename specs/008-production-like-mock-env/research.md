# Research: Production-Like Mock Environment

## R1 How the function runs
**Decision**: Run `sam build` output in the `public.ecr.aws/lambda/python:3.13` image (it contains the Lambda runtime interface emulator). Environment, `AWS_LAMBDA_FUNCTION_TIMEOUT`, and the container `mem_limit` come from the template via `render_env.py`; function name is `<stack>-fn` as in the template.
**Rationale**: same interpreter, same handler path, same packaged dependencies (python-tds, zeep, IDP SDK) as production, so the "missing module" class of failure appears here.
**Alternatives**: importing `lambda_handler` in-process (current tests; hides packaging); `sam local start-api` (needs more of the template and does not enforce our limits the same way).

## R2 Redirecting AWS calls
**Decision**: boto3 honours `AWS_ENDPOINT_URL`; set it to the edge. Fake static credentials; region `us-gov-east-1`; partition-aware ARNs are preserved because the stack outputs, secret ARN and keys are seeded with `arn:aws-us-gov:` values.
**Rationale**: no code change in the function (FR-004).
**Risk**: Moto's ARN generation uses `aws`; the seed script creates resources then the edge rewrites ARNs in responses for the services where the function echoes them (secrets, KMS). Covered by a parity test.

## R3 AWS emulation: Moto behind an edge
**Decision**: Moto server for DynamoDB, Secrets Manager, S3, KMS, CloudFormation (describe stacks). A Python edge proxy in front: (1) records every call, (2) applies faults, (3) checks the call against the function role's statements from the template, (4) handles Lambda `Invoke` itself.
**Lambda Invoke**: target = this function -> POST to the runtime emulator (`Event` returns 202 and runs in the background, `RequestResponse` waits); target = IDP `LookupFunction` -> the stub in `mock/idp`.
**Alternatives**: LocalStack (IAM enforcement and Lambda are paid/partial, GovCloud partition not first class); real AWS sandbox (violates "no cloud account", SC-001/SC-005).

## R4 Permission parity (FR-008)
**Decision**: `render_env.py` extracts the `DirectFunction` policy statements (resolving `!Sub`, parameters and conditions with the existing `tests/cfn_eval.py` evaluator). The edge maps each request to an IAM action (`actions.py`) and resource and evaluates Allow/Deny with glob matching; no match -> an `AccessDenied` body worded like AWS (`User: <role arn> is not authorized to perform: <action> on resource: <arn>`).
**Rationale**: reproduces the `LookupFunction` and `kms:Decrypt` denials seen in production using the template itself.
**Limit**: key policies, SCPs and permission boundaries are not modelled (documented fidelity gap).

## R5 SyteLine SOAP mock
**Decision**: Werkzeug app serving a WSDL and the three operations the client uses (`LoadDataSet`, `SaveDataSet`, `CallMethod`) with in-memory IDO collections seeded per scenario. Request/response shapes taken from `src/syteline/client.py` and `idos.py`; row-level rejection and session-expiry replies are scriptable faults. Every request stored as a call record.
**Alternatives**: reuse `FakeSoap` over HTTP (shares its blind spots: it was written from the same assumptions as the client).

## R6 SQL Server (FR-014: real engine)
**Decision**: `mssql/server:2022-latest` with TLS enabled (`forceencryption`), per-run CA and server cert whose SAN is the compose host name; the CA PEM is stored in the mock secret as `SQL_CA_PEM`, so the function's verified-TLS path is exercised. `init.sql` creates `LongList`, `Infobar`, `custaddr`, `co` and **stand-in** `NextCoSp` / `NextWOSp` that allocate from a sequence table and return `@Key`/`@Infobar` in the shape the function reads.
**Honesty**: the routines are stand-ins for SyteLine's; their real logic is a documented fidelity gap.
**Apple silicon**: x86 image under emulation; start-up up to 90 s; health check gates the suite.
**Faults**: stop the container (unreachable), change the login (refused), set the routine to raise (rejected), `WAITFOR DELAY` (timeout / uncertain).

## R7 IDP read path
**Decision**: seed Moto with what `idp_sdk` reads (CloudFormation stack outputs for `<IdpStackName>`, tracking-table item, S3 objects under the output bucket) and serve the `LookupFunction` from the stub. The exact shapes are taken from the SDK source in the sibling repository `accelerated-intelligent-document-processing-on-aws` (the same one `IDP_REPO` points at) and checked by a test that reads each seeded document through the real SDK.
**Scenarios**: `passes-review`, `needs-review` (confidence below threshold, from the real `159112.pdf` shape but synthetic values), `incomplete` (no `po_number`/`lines`), `already-processed`, `not-found`.
**Faults**: `idp.kms_denied`, `idp.lookup_denied` come from R4 plus an explicit fault for cases the policy cannot express.

## R8 Schedule and API entry
**Decision**: `driver/app.py` posts `{"action":"sweep"}` to the runtime on a configurable interval (default 20 s, documented as compressed from 5 min) and exposes an HTTP endpoint that turns a request into an API-Gateway proxy event using the route table from the template's `OrderApi` definition.

## R9 Control API and tests
**Decision**: one JSON control API on the edge (`/__mock__/…`, see contracts). pytest talks only to it and to the function endpoint.
**Guard**: `conftest.py` refuses to run unless every address in the function env resolves to a compose service name or loopback (FR-011), and asserts `SYTELINE_LIVE_WRITES` is false.

## R10 Isolation and no-egress (SC-005)
**Decision**: the compose network is `internal: true` (no route out); the control and function ports are published to loopback only. `test_isolation.py` attempts an outbound connection from inside the function container and expects failure.

## R11 Drift detection (FR-012)
**Decision**: `test_parity.py` compares (a) function env names in the template with what the container received, (b) the actions in `lambda_function.py` with the ones the driver and docs list, (c) the template timeout/memory with the container's limits, (d) the API routes with the shim's table. Any difference fails the test.

## R12 Environment limits of this workspace
Docker is **not installed in the authoring environment**, so the container stack cannot be run during implementation here. The pure-Python parts (policy evaluator, action table, template renderer, SOAP mock handlers, control-API logic) are unit-tested in-process. The full stack is verified by the quickstart on a machine with Docker; tasks mark this explicitly and the completion report must say so.

## R13 Resets (FR-010)
**Decision**: `reset` = Moto `/moto-api/reset` + re-seed, truncate and re-seed SQL tables, clear ERP state and call records, clear faults and locks, and (optionally) restart the runtime container to drop warm state. The jobs table is re-created by the seeder with the template's key schema.

## Changes made during implementation

- **Moto runs inside the edge process** (one container fewer than planned); the plan's separate `moto` service is gone.
- **SQL faults** use a TCP front (`edge/sqlproxy.py`, alias `sqlserver` on the internal network, forwarding to `sqlserver-real`) plus
  `sa` statements (`edge/sqlcontrol.py`: disable the login, set flags the stand-in routines read), instead of controlling the container.
  The TLS session stays between the function and the real engine.
- **Three function containers** (`fn1`..`fn3`): the runtime emulator handles one invocation at a time, the real service scales out.
- **Live writes are ON inside the mock** (`SYTELINE_LIVE_WRITES=true`) so a save can run; `edge.prepare` and the suite refuse to run
  unless the ERP and AWS addresses are mock addresses. This replaces the plan's "assert live writes are false".
- **FIPS endpoints are off** (`AWS_USE_FIPS_ENDPOINT=false`): botocore rejects FIPS with a custom endpoint URL.
- **DynamoDB transactions** are checked per item (`actions.identify_all`), as IAM does.
- **Findings in the function** surfaced by the mock: unaliased `ttl` in update expressions (fixed, `#ttl`), and the zeep list behaviour
  for an `ArrayOfString` parameter (documented, not changed).
