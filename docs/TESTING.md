# Testing the deployed Lambda, step by step

A sequence that goes from no risk to live writes. Steps 1 to 6 change nothing in SyteLine; steps 7 and 8 reserve numbers (a real change); step 9 writes an order. Do steps 7 onward only in a test window approved by the ERP owner.

> The commands below have not been run against a real account: treat them as a checklist and adjust names. Names marked UNCONFIRMED in [DATABASE.md](DATABASE.md) and [RESERVATIONS.md](RESERVATIONS.md) must be verified read-only (step 5) before any live write.

## Step 0: Local checks (no AWS)

Before deploying you can also rehearse the whole intake, and the failures seen in production, against a production-like mock
environment on your machine (containers, no AWS account): [MOCK_ENVIRONMENT.md](MOCK_ENVIRONMENT.md). A pass there is not proof of
production behaviour.

```
pip install -r src/requirements-dev.txt pytest        # pytest and rapidfuzz
python -m pytest tests -q
cfn-lint src/template.yaml
(cd src && sam validate --lint -t template.yaml)
```
Expect only the failures that already exist: 2 in `tests/contract/test_health.py` (they import `zeep`, which the local test environment may not have). Nothing else may fail.

## Step 1: Gather what the deployment needs
- **ERP secret** (Secrets Manager, JSON): `SYTELINE_PASS` and the database keys `SQL_SERVER`, `SQL_DATABASE`, `SQL_USER`, `SQL_PASS`, `SQL_CA_PEM` (optional `SQL_PORT`); you can put them in `deploy.env` and let `./deploy.sh` merge them into the secret (step 2). `SQL_CA_PEM` is needed in the default (encrypted) mode; if the plant server has no verifiable certificate, deploy with `SQL_ENCRYPT=false` instead (no CA needed, the connection is unencrypted, warnings appear: see [DATABASE.md](DATABASE.md)). See [DATABASE.md](DATABASE.md).
- **IDP stack:** its name, output bucket and tracking table, and one test document that has finished processing and whose result contains the fields named in `src/syteline/idp_fields.py` (or set `IDP_FIELD_MAP`). See [RESERVATIONS.md](RESERVATIONS.md).
- **Network:** subnets with routes to the plant web service (TCP 80) and the database (TCP 1433), and private paths to S3, CloudFormation, DynamoDB and Secrets Manager; an execute-api VPC endpoint if you will call the API.
- **A checkout of the IDP accelerator** (`IDP_REPO`).
- **Written approval** from the ERP owner for steps 7 onward.

## Step 2: Deploy with live writes off
Copy `src/deploy.env.example` to `src/deploy.env` and fill in the ERP settings, the database login (`SQL_SERVER`, `SQL_DATABASE`, `SQL_USER`, `SQL_PASS`, `SQL_PORT`, `SQL_CA_PEM_FILE`: `deploy.sh` merges them into the secret, asks first, `--yes` skips the question), the network settings, the four route settings (leave `database` unless the web service has an endpoint for the operation), the `IDP_*` settings and `LIVE_WRITES=false`. Then:
```
cd src
IDP_REPO=/path/to/accelerated-intelligent-document-processing-on-aws ./deploy.sh
```
Find the function name (it is the stack's only function, `<stack>-fn`):
```
aws cloudformation describe-stacks --stack-name sytline-connect --query 'Stacks[0].Outputs'
aws cloudformation list-stack-resources --stack-name sytline-connect \
  --query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function'].[LogicalResourceId,PhysicalResourceId]" --output table
```
You need `DirectFunction` (output `DirectFunctionName`): every call below goes to it. The API routes are simulated by an API-shaped payload (`httpMethod`, `resource`, ...); the function routes it exactly as API Gateway's request would be.

## Step 3: Credential-free health check
```
aws lambda invoke --function-name <DirectFunction> --cli-binary-format raw-in-base64-out \
  --payload '{"action":"health","dry_run":true}' out.json && cat out.json
```
Expect `routes` listing every operation and the stages `load`, `dns`, `tcp`, `wsdl` passing. No credentials are sent and no database connection is made.

## Step 4: Full health check, including the database
```
aws lambda invoke --function-name <DirectFunction> --cli-binary-format raw-in-base64-out \
  --payload '{"action":"health","diagnose":true}' out.json && cat out.json
```
Expect `token`, and, when any operation uses the database, `db_dns`, `db_tcp` and `db_login` passing. Fix the first failing stage before going on:

| Failure | Meaning |
|---|---|
| `db_dns` | the database name does not resolve from the function's network |
| `db_tcp` | no route or security-group rule to the database port |
| `db_login`: `certificate not trusted` | `SQL_CA_PEM` is not the server's authority (or deploy with `SQL_ENCRYPT=false`) |
| `db_login`: `host name does not match the certificate` | use the certificate's name, or set `SYTELINE_SQL_VERIFY_HOSTNAME=false` (only the host-name check is switched off) |
| `db_login`: `login refused` | wrong login or password |
| `db_login`: `database not configured: <keys>` | those keys are missing from the secret |

`diagnostics.findings` explains name-resolution and connection problems for the web service and, when the health check tests the database, for the database host (`diagnostics.database`; see DATABASE.md).

## Step 5: Verify the unconfirmed names, read-only
Before any live write, check against the plant: the `LongList` and `Infobar` types and the `NextCoSp` / `NextWOSp` signatures, the `custaddr` and `co` column names, the `SLCos` terms and ship-via property names, and the other names in `src/syteline/idos.py`. Correct any that differ.

## Step 6: Read a note (read-only)
```
aws lambda invoke --function-name <DirectFunction> --cli-binary-format raw-in-base64-out \
  --payload '{"action":"get_notes","record_type":"co","record_key":"<an existing CO number>"}' out.json && cat out.json
```

## Step 7: Reserve with live writes still off
Invoke the function with an API-shaped reserve request (no API Gateway needed). Use one fixed caller ARN throughout: a save is only accepted from the caller that reserved.
```
ARN=arn:aws-us-gov:iam::<account>:role/TestCaller
cat > ev.json <<E2
{"httpMethod": "POST", "resource": "/job-reservations",
 "body": "{\"request_ref\":\"test-1\",\"confirmed\":true,\"uploaded_by\":\"tester\",\"document_id\":\"<batch/doc.pdf>\"}",
 "requestContext": {"identity": {"userArn": "$ARN"}}}
E2
aws lambda invoke --function-name <DirectFunction> --cli-binary-format raw-in-base64-out --payload file://ev.json out.json && cat out.json
```
Expect `422 LIVE_WRITES_DISABLED`: the IDP document was read and the customer matched, and no number was reserved. Any other error tells you what to fix:

| Response | Meaning |
|---|---|
| `DOCUMENT_NOT_FOUND`, `DOCUMENT_NOT_READY`, `DOCUMENT_INCOMPLETE`, `DOCUMENT_NEEDS_REVIEW`, `IDP_UNAVAILABLE` | the IDP document or access to it (`fields` names the fields) |
| `CUSTOMER_AMBIGUOUS`, `CUSTOMER_LOW_CONFIDENCE`, `CUSTOMER_NOT_FOUND` | the customer match; `candidates` shows the best matches and scores |
| `DATABASE_NOT_CONFIGURED` (503) | a `SQL_*` key is missing from the secret (`SQL_CA_PEM` counts only while encrypted) |

## Step 8: Reserve with live writes on (approved test window)
Redeploy with `LIVE_WRITES=true` and `LIVE_APPROVAL_REF=<the ERP owner's reference>`, then repeat step 7 with a new `request_ref`. Expect `201`, `state: reserved`, `co_num` and one `job` per line. In SyteLine check that the numbers advanced and nothing else changed. Repeating the same `request_ref` returns the same numbers without reserving new ones.

## Step 9: Save, then check the result
```
cat > save.json <<E2
{"httpMethod": "POST", "resource": "/job-reservations/{id}/save", "pathParameters": {"id": "<reservation_id>"},
 "body": "{\"confirmed\":true,\"uploaded_by\":\"tester\",\"document_id\":\"<batch/doc.pdf>\"}",
 "requestContext": {"identity": {"userArn": "$ARN"}}}
E2
aws lambda invoke --function-name <DirectFunction> --cli-binary-format raw-in-base64-out --payload file://save.json out.json && cat out.json
```
Expect `202 save_accepted`. The worker then writes in the background; poll:
```
aws lambda invoke --function-name <DirectFunction> --cli-binary-format raw-in-base64-out \
  --payload '{"httpMethod":"GET","resource":"/job-reservations/{id}","pathParameters":{"id":"<reservation_id>"}}' out.json && cat out.json
```
Wait for `saved` (or investigate `partly_saved`, `failed`, `needs_reconciliation`: see [SAVE_WORKFLOW.md](SAVE_WORKFLOW.md)). Read any `warnings` (for example that the customer's defaults were not available). In SyteLine verify: the order header (with the customer's terms and ship-via), the lines each referencing their job number, the jobs linked back to their lines, and the teardown dates.

## Step 10: Refusal tests (each must write nothing)

| Test | Expected |
|---|---|
| Send the same save again | `409 RESERVATION_USED` |
| Save with a different `document_id` | `422 RESERVATION_MISMATCH` |
| Save with another caller ARN or another `uploaded_by` | `403 RESERVATION_FORBIDDEN` |
| Use a document still in review, or with a low-confidence field | `409 DOCUMENT_NOT_READY` / `422 DOCUMENT_NEEDS_REVIEW` |
| Remove `SQL_CA_PEM` from the secret (default encrypted mode) and reserve | `503 DATABASE_NOT_CONFIGURED`, nothing reserved, no connection made |
| Deploy with `SQL_ENCRYPT=false` and run the health check | `db_login` says `UNENCRYPTED` and `warnings` carries the unencrypted warning; the logs show it on every connection |
| Reserve for a customer that does not match | `422` with the candidates; nothing reserved |

## Step 11: Through the real API (optional)
The API is private: call it from inside the VPC or through the execute-api endpoint with an IAM-signed request (for example `awscurl --service execute-api`). Routes: `POST /job-reservations`, `POST /job-reservations/{id}/save`, `GET /job-reservations/{id}` (and `POST /confirmed-orders` for the one-step upload). The API ID is the `ApiId` stack output. The caller identity is then the signing principal.

## Step 11b: Start from the IDP's finished-document event (optional)

`aws lambda invoke --function-name <DirectFunctionName> --payload fileb://sample_events.json out.json` (no `action`: the intake is the default action): with live writes off the response is `refused` (a field below its threshold) or `read_only`, with the scan result `summary` and nothing reserved. See [EVENT_INTAKE.md](EVENT_INTAKE.md) for the outcomes and for switching on the automatic rule (`IDP_STATE_MACHINE_ARN`).

## Step 12: Check the records and stop
- **CloudWatch logs:** each function writes one structured line per request, and each database step its own line (type, outcome, duration, counts). Confirm no password or login appears.
- **DynamoDB** (`JobsTableName` output): reservation items (`RESERVATION#<id>`) hold the document ID, line count, numbers and results, and no customer, PO number or lines.
- **After the window:** redeploy with `LIVE_WRITES=false`.

A save that ends `needs_reconciliation` or `partly_saved` is never retried or deleted automatically: check SyteLine for the rows listed in `possibly_written`.
