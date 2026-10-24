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

## Real-IDP mode (FR-017..FR-020)

### R14 Where the real/mock decision is made
**Decision**: in the edge, per request, after the permission check. A request is "IDP-bound" when it names the configured IDP stack
(`cloudformation:*` with `StackName`), the output bucket (`s3` path), the tracking table (`dynamodb` `TableName`) or a function named
`<stack>-LookupFunction-*` (`lambda:Invoke`). Those go to the real service in real mode; all else is served by the mock as before.
**Rationale**: one decision point that already records calls and applies the template's permissions (FR-019); the function is unchanged.
**Alternatives**: giving the function real endpoints for the IDP services (it would hold the credentials, FR-018); a second proxy (duplicate logging).

### R15 Re-signing
**Decision**: the edge drops the function's (fake) signature headers and signs the request with the developer's credentials using
botocore's `SigV4Auth` (`S3SigV4Auth` for S3), then sends it to the service's real endpoint:
`https://<service>[-fips].<region>.amazonaws.com` (FIPS by default for `aws-us-gov`, `MOCK_IDP_FIPS=false` to turn off).
**Rationale**: the function never sees the credentials; the edge already has egress on the `outside` network.
**Risk**: re-signing correctness is only verifiable against real AWS (unit tests use a fake endpoint that checks the key id and token).

### R16 Credentials
**Decision**: `make -C mock up MOCK_IDP=real MOCK_IDP_PROFILE=<profile> ...` runs `aws configure export-credentials --profile <p> --format env-no-export`
(works for SSO and assumed roles) and writes `mock/.idp/creds` (mode 0600) with the key, secret, token and expiry. Only the edge mounts
`mock/.idp`. The edge re-reads the file on every real request, so `make -C mock idp-creds` refreshes without a restart. Values are never
printed; call records hold no header or body of real requests beyond the operation and resource.
**Alternatives**: environment variables on the edge (visible in `docker inspect`); mounting `~/.aws` (exposes every profile).

### R17 Account, region and the template's permissions
**Decision**: the template's permission resources contain `${AWS::AccountId}`; in real mode `edge.prepare` calls `aws sts get-caller-identity`
with the profile, then renders with that account id, the IDP region and partition (`MOCK_IDP_REGION`, default `us-gov-east-1`), and the
real stack, bucket, table and key ARNs (`MOCK_IDP_STACK`, `MOCK_IDP_BUCKET`, `MOCK_IDP_TABLE`, `MOCK_IDP_KMS_KEYS`). `render_env.render()` takes these as
parameters, so the policy check still comes from the template.

### R18 Read-only allow-list on the real side
**Decision**: only these go out: `cloudformation:DescribeStacks|ListStackResources|DescribeStackResource|DescribeStackResources`,
`s3:GetObject`, `dynamodb:GetItem|Query|BatchGetItem`, `lambda:InvokeFunction` on `<stack>-LookupFunction-*`. Anything else IDP-bound is refused with
an AWS-worded denial and recorded with `target: real, allowed: false`. Calls to any other real service are never made.
**Note**: invoking the lookup function runs the IDP's own read-only status lookup; this is the only call that executes code on the real side.

### R19 Decryption
**Decision**: no mock KMS check in real mode. The real S3 and DynamoDB decrypt server-side, so the real key policy and the developer's
own KMS permissions apply: the production `kms:Decrypt` failure reproduces for real, including key-policy denials the mock cannot model.

### R20 Picking the document and the scenario
**Decision**: `MOCK_IDP_DOCUMENT=<id>` is required in real mode. `edge/events.py` builds the processing-complete event from the sample
event's shape. The mode exposes one scenario, `real-idp` (`GET /scenarios` lists only it; mock scenarios are refused with a message, so the modes
never mix). The mock database and ERP start empty, so a real document usually ends `refused` at the customer step: that is a reported outcome, not
a failure. Seeding a matching customer is a follow-up (`MOCK_IDP_SEED_CUSTOMER`), out of scope here.

### R21 Credential problems
**Decision**: `/__mock__/health` reports `idp` as `up`, or the reason (`credentials missing`, `credentials expired at <time>`, `profile could not
export credentials`). The edge checks the file every 10 seconds, so the readiness wait and `make -C mock up` stop within 30 seconds with that
reason (SC-007). A real request with expired credentials answers an AWS-worded `ExpiredToken` error and is recorded; nothing falls back to the mock IDP.
