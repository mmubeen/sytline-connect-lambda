# Deploying with the SAM CLI

Target: AWS GovCloud **us-gov-east-1**, one stack named `sytline-connect`, one configuration (`default`). Everything below is run
from the `src/` directory. The deployment creates **one Lambda function** and **no network resource**: it attaches that function
to a VPC, subnets and security group(s) that already exist. The network and ERP prerequisites are in section 2 and come from
`docs/Instruction Sheet AWS GovCloud API Gateway to On-Premises Sy.docx`.

> **Status:** the template builds and validates locally, but it has not yet been deployed to a real account.
> The SyteLine web-service names (collections, methods, SOAP operations) are unconfirmed until the read-only
> discovery task (T013 in `specs/001-syteline-erp-integration/tasks.md`) has run. **Keep live ERP writes off.**

> **Moving from an earlier shape** (eight functions, a network created by the stack, or the stand-alone stack)? Read [MIGRATION.md](MIGRATION.md) **before** deploying.

> Related: the job-number workflow [RESERVATIONS.md](RESERVATIONS.md), the IDP event intake [EVENT_INTAKE.md](EVENT_INTAKE.md), the SyteLine database calls [DATABASE.md](DATABASE.md), testing a deployment step by step [TESTING.md](TESTING.md).

## 1. What gets deployed

| Resource | Purpose |
|---|---|
| `DirectFunction` (`<stack>-fn`, in your VPC, timeout 300 s, no reserved concurrency, no automatic retries of asynchronous invokes) | **the one function**: the request decides what runs (see below) |
| `OrderApi` (private REST API, stage `prod`; **only if `API_VPCE_ID` is set**) | `POST /confirmed-orders`, `GET /upload-jobs/{id}`, `POST /job-reservations`, `POST /job-reservations/{id}/save`, `GET /job-reservations/{id}`; IAM auth; callable only through your existing `execute-api` VPC endpoint; every route goes to the one function |
| Schedule (every 5 minutes) | starts the cleanup action on the same function |
| Document event rule (**only if `IDP_STATE_MACHINE_ARN` is set**) | starts the same function from the IDP's finished-document event |
| `DepsLayer` (`sytline-connect-deps`) | zeep, lxml, requests, urllib3 and the rest, built for Linux x86_64 / Python 3.13 |
| `JobsTable` (DynamoDB, on demand, point-in-time recovery, TTL, **retained**) | uploads, reservations, event claims and the ERP-write lock; the stack can never delete or replace it |
| IAM role | one role for the function: the union of what the former eight functions needed (secret read, the jobs table, starting itself, reading the IDP) |

**Never created:** any VPC, subnet, route table, internet or NAT gateway, VPC endpoint, security group or security-group rule.

What runs is decided by the request: an API route (the five above), an `action` (`health`, `add_note`, `get_notes`, `document_event`, and the internal `work_upload`, `work_save`, `sweep` that the function starts on itself), or, with no `action`, the IDP finished-document event. The health check must be asked for with `{"action": "health"}`. Details: [`specs/007-single-function-existing-vpc/contracts/dispatch.md`](../specs/007-single-function-existing-vpc/contracts/dispatch.md).

ERP writes stay one at a time: background uploads and saves take a lease lock kept in the jobs table before they claim a job; if the lock is busy they leave the job queued and the 5-minute cleanup starts them again. Quick requests (health, status, reservations) are never queued behind a save.

## 2. Prerequisites

**Tools (on the machine that deploys)**
- AWS CLI v2 with credentials for the GovCloud account, ideally as a named profile (`aws configure list-profiles`; the `aws-us-gov` partition). Select it with `./deploy.sh --profile <name>` (section 4).
- SAM CLI (`sam --version`): <https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html>
  or `pip install aws-sam-cli`.
- `python3` with `pip` (any recent 3.x) and `make`. **Docker and a local Python 3.13 are not needed**: `src/Makefile`
  installs Linux wheels with pip's cross-platform flags.
- IAM permissions to create CloudFormation stacks, Lambda, API Gateway, DynamoDB, EventBridge and IAM roles, and an S3 bucket for artifacts (SAM creates and manages that bucket: `resolve_s3 = true`).

**In the account (created before the first deploy)**
1. **Secret.** A Secrets Manager secret in us-gov-east-1 containing JSON with `SYTELINE_PASS`:
   ```
   printf '{"SYTELINE_PASS":"<password>"}' > /tmp/sytline-secret.json   # do not commit; delete afterwards
   aws secretsmanager create-secret --region us-gov-east-1 --name sytline/connect \
       --secret-string file:///tmp/sytline-secret.json
   shred -u /tmp/sytline-secret.json 2>/dev/null || rm -P /tmp/sytline-secret.json
   ```
   Note the ARN it prints (`arn:aws-us-gov:secretsmanager:us-gov-east-1:...`). The database login can be merged into it by `deploy.sh` ([DATABASE.md](DATABASE.md)).
2. **The network** (section 3a): a VPC, subnets and security group(s), made and owned by the network team.
3. **The API client path (optional).** If you want the private API: an `execute-api` **interface VPC endpoint** in the VPC the approved callers use; note its ID (`vpce-...`). The stack restricts the API to it and never creates it. Without one, set no `API_VPCE_ID` and there is no API.

## 3. Configure

`src/samconfig.toml` holds only the fixed settings (stack name, region, capabilities). Site-specific values live in
`src/deploy.env`, which is git-ignored and contains no passwords:

```
cp deploy.env.example deploy.env
```

| Variable (`deploy.env`) | Template parameter | Meaning |
|---|---|---|
| `AWS_PROFILE_NAME` | (none) | AWS credentials profile for every AWS-facing step; `--profile` overrides it; empty = default credential chain |
| `SYTELINE_WSDL` | `SytelineWsdl` | `http://<host>/IDORequestService/IDOWebService.asmx?wsdl` (HTTP, port 80) |
| `SYTELINE_USER` | `SytelineUser` | web-service user name |
| `SYTELINE_SECRET_ARN` | `SytelineSecretArn` | the secret from step 2.1 |
| `SYTELINE_CONFIG` | `SytelineConfig` | SyteLine configuration, default `CCT` |
| `SYTELINE_DEFAULT_WHSE`, `SYTELINE_DEFAULT_ORDTYPE` | `DefaultWhse`, `DefaultOrdType` | used when an order omits them (and by the event intake) |
| `WORKER_VPC_ID` | `WorkerVpcId` | **required**: the existing VPC (`vpc-...`) |
| `WORKER_SUBNET_IDS` | `WorkerSubnetIds` | **required**: existing subnets of that VPC, comma-separated, no spaces (one is allowed but is not highly available; two in different Availability Zones recommended) |
| `WORKER_SG_IDS` | `WorkerSecurityGroupIds` | **required**: existing security groups, comma-separated |
| `API_VPCE_ID` | `ApiVpcEndpointId` | **optional**: the existing `execute-api` interface endpoint (`vpce-...`) the private API is reachable through. **Empty = no API is created** (the function is then reached by direct invocation, the IDP event and its own schedule only). Always passed, so emptying it later removes the API |
| `IDP_STATE_MACHINE_ARN` | `IdpStateMachineArn` | optional: the IDP state machine whose `SUCCEEDED` events start the event intake (same account and region; needs the IDP settings); empty = nothing starts automatically. See [EVENT_INTAKE.md](EVENT_INTAKE.md) |
| `LIVE_WRITES`, `LIVE_APPROVAL_REF` | `LiveWrites` | leave `false`; see section 7 |
| route, database, IDP and tuning settings | various | optional; see [DATABASE.md](DATABASE.md), [RESERVATIONS.md](RESERVATIONS.md) |

Settings of the earlier shapes (`WORKER_ROUTE_TABLE_IDS`, `HOST_NETWORK_CIDR`, `PLANT_CIDR_*`, `DYNAMODB_PREFIX_LIST_ID`, `CREATE_VPC_ENDPOINTS`, `CREATE_API_ENDPOINT`, `ATTACH_*`, `ALLOW_PLAIN_HTTP`, `VERIFY_TLS`, `DATABASE_CHECK`) are no longer used: `deploy.sh` prints a notice naming any it finds in `deploy.env` and does not pass them.

## 3a. The network this deployment needs (and does not create)

The deployment only **attaches** the function to the network you give it. That network must already provide:

| Need | Why |
|---|---|
| a route to the plant over the VPN, outbound **HTTP 80** to the SyteLine web service, plant DNS that resolves the SyteLine host | the ERP calls |
| outbound **TCP 1433** to the SQL Server (and a route to it) | the database calls ([DATABASE.md](DATABASE.md)), only when an operation uses the database |
| a private path to **Secrets Manager** and **DynamoDB** (VPC endpoints with private DNS, or a NAT route) and outbound HTTPS from the security group | the credentials and the jobs table |
| a private path to **S3 and CloudFormation** (and the IDP's tracking table) | reading documents through the IDP SDK, when the IDP is configured |
| the `execute-api` interface endpoint (`API_VPCE_ID`), only if you want the API | callers reach the private API through it |

This deployment adds **no rule** to your security groups and changes nothing in your network. If a path is missing, the health check names the stage that fails (`dns`, `tcp`, `wsdl`, `token`, `db_*`) and `"diagnose": true` explains why (section 5).

## 4. Validate, build, deploy

```
./deploy.sh --profile <govcloud-profile> --validate   # sam validate --lint
./deploy.sh --profile <govcloud-profile> --build      # sam build -> .aws-sam/build (layer + the function)
./deploy.sh --profile <govcloud-profile>              # build + sam deploy, with an interactive change-set review
```

`--profile NAME` (or `AWS_PROFILE_NAME` in `deploy.env`; the flag wins) is passed to `sam build`, `sam validate`, `sam deploy` and the existing-stack check. `deploy.sh` refuses a profile that `aws configure list-profiles` does not list, and a name with unsafe characters. With no profile the default credential chain is used. The profile is printed at the start of each run so you can see which account you are about to change.

`deploy.sh` stops before building, naming the value, if `deploy.env` is missing, a required value (including any of the four network identifiers) is empty or malformed. It warns when only one subnet is given, and prints a notice when the existing stack still has functions of the earlier shape (read [MIGRATION.md](MIGRATION.md) before confirming that change set). Equivalent raw commands: `sam validate --lint`, `sam build`, `sam deploy --parameter-overrides 'Key="value"' ...` (quote every value; list parameters such as subnets are comma-separated inside the quotes).

Review the change set before confirming. On the first deploy expect: the layer, the table, the API, the one function (and its role, the schedule) and, if `IDP_STATE_MACHINE_ARN` is set, the event rule. Later deploys change only what differs.

## 5. Verify after deploying

Stack outputs (API id, function names, table name):
```
aws cloudformation describe-stacks --region us-gov-east-1 --stack-name sytline-connect \
    --query 'Stacks[0].Outputs' --output table
```

1. **Dry-run health check** (no credentials, no login; proves the code loads and the web-service host is reachable):
   ```
   aws lambda invoke --region us-gov-east-1 --function-name <DirectFunctionName> \
       --payload '{"action":"health","dry_run":true}' --cli-binary-format raw-in-base64-out out.json && cat out.json
   ```
   Expected stages: `load`, `dns`, `tcp`, `wsdl`. A failing stage names where connectivity breaks (DNS, routing, security group, VPN).
   **To find out why a stage fails, add `"diagnose": true`** to the payload (`'{"action":"health","dry_run":true,"diagnose":true}'`): the result then carries a separate `diagnostics` section (resolver settings, the ERP name lookup with retries, a connection test to each ERP address, the same for the function's regional AWS endpoint, and plain-language `findings`). It contacts only the ERP host and that AWS endpoint (and the database host when the health check tests the database: see [DATABASE.md](DATABASE.md)) and sends no data or credentials; the stages are unchanged.
2. **Full health check** (logs in with the secret): same call with `'{"action":"health"}'`; expected stages `dns`, `tcp`, `wsdl`, `token`.
3. **API from an approved client** (only when `API_VPCE_ID` is set; inside the VPC that has the `execute-api` endpoint, with IAM credentials):
   ```
   awscurl --service execute-api --region us-gov-east-1 -X POST \
       -H 'Content-Type: application/json' -d @order.json \
       https://<ApiId>-<API_VPCE_ID>.execute-api.us-gov-east-1.amazonaws.com/prod/confirmed-orders
   ```
   (request body: `specs/001-syteline-erp-integration/contracts/api.md`). Expect HTTP 202 and an `upload_id`; then
   `GET .../prod/upload-jobs/<upload_id>`. From outside the approved endpoint or without IAM credentials the call must be denied.
4. **Expected outcome while live writes are off:** the upload ends `failed` with code `LIVE_WRITES_DISABLED`, after
   the read-only customer lookup and **before** any number is allocated or any row is written. If the lookup itself
   fails (for example `ERP_UNREACHABLE`), the unconfirmed web-service names in `src/syteline/idos.py` are the first
   thing to check. That is what the discovery task is for.

Logs: `sam logs --stack-name sytline-connect --region us-gov-east-1 --name DirectFunction --tail`. Each request writes one structured line
(type, identifiers, outcome, duration). Passwords and tokens are never logged.

## 6. Updating

Change code or `deploy.env`, then run `./deploy.sh` again. SAM rebuilds, shows the change set and updates the stack.
Uploads and reservations already stored are kept; the table is retained and never replaced by code changes.

## 7. Enabling live ERP writes (only for an approved test)

There is no test SyteLine. Live writes (number allocation, inserts, note creation) are off by default and must only be
enabled for a test the ERP owner approved in writing, with a reconciliation and cleanup plan
(`docs/reconciliation-runbook.md`; `specs/001-syteline-erp-integration/quickstart.md` step 5).

1. In `deploy.env` set `LIVE_WRITES=true` **and** `LIVE_APPROVAL_REF=<the approval reference>`. `deploy.sh` refuses
   `LIVE_WRITES=true` without a reference.
2. `./deploy.sh`; run the single approved test.
3. Set `LIVE_WRITES=false` (and clear the reference), `./deploy.sh` again, and confirm with a new upload that it ends
   `LIVE_WRITES_DISABLED`.

## 8. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `sam build` fails in `pip install` | pip too old for `--platform`: `python3 -m pip install --upgrade pip`; or no network access to the package index |
| `deploy.sh`: "missing in deploy.env" | fill in the named values (the four network settings are all required) |
| `deploy.sh`: "WORKER_SUBNET_IDS must be ..." (or another identifier) | a network identifier is malformed: no spaces, `subnet-...,subnet-...`, `sg-...`, `vpce-...` |
| `deploy.sh`: "AWS profile ... is not configured" | the name is not in `aws configure list-profiles`; fix the name or run `aws configure --profile <name>` |
| `deploy.sh`: "SYTELINE_SECRET_ARN is not a complete secret ARN" | the ARN still has the `ACCOUNT` placeholder, a stray space or carriage return, or is cut off |
| Stack in `ROLLBACK_COMPLETE` after a failed first deploy | `sam delete --stack-name sytline-connect --region us-gov-east-1`, fix the cause, redeploy |
| The change set removes a VPC or several functions | the stack is of an earlier shape: stop and read [MIGRATION.md](MIGRATION.md) |
| Background work never starts, jobs stay `accepted` | the function cannot invoke itself (the role statement) or is throttled; check the logs for `work_upload` / `work_save`, and that the 5-minute cleanup runs |
| The function times out reaching DynamoDB or Secrets Manager | no endpoint or NAT path from the subnets (section 3a); check the security group allows HTTPS out |
| `health` fails at `dns` or `tcp` | plant DNS not resolvable from the subnets, VPN route missing, or the security group blocks outbound port 80 |
| `health` fails at `token` | wrong user, or wrong password in the secret; check the secret JSON key is `SYTELINE_PASS` |
| `{"action":"health"}` works but `{}` is refused with `EVENT_INVALID` | expected: with no `action` the function expects an IDP event; ask for the health check explicitly |
| API call returns 403 | not coming through the approved VPC endpoint, or missing/incorrect IAM credentials |
| Upload ends `LIVE_WRITES_DISABLED` | expected until an approved live test (section 7) |
| Upload ends `needs_reconciliation` | follow `docs/reconciliation-runbook.md`; never resubmit the same order until its records are reconciled |

## 9. Removing the stack

```
sam delete --stack-name sytline-connect --region us-gov-east-1
```
The stack creates no network resource, so nothing in your network is touched. **The jobs table is retained**: it is not deleted with the stack (it keeps every upload and reservation record); delete it yourself only when you are sure you no longer need its records. The Secrets Manager secret is not part of the stack and is not deleted either.
