# Event intake: reading a finished IDP document and reserving its job numbers

The IDP document-processing service (the accelerator) emits an EventBridge event, "Step Functions Execution Status Change", whenever a document's processing changes state. The plant stack's direct-invoke Lambda (`DirectFunction`) runs it as the **`document_event`** action, which is its **default**: it is started by the `SUCCEEDED` event of that state machine, or invoked directly with the event. There is no separate function. It never trusts the event's content: only the **document ID** is taken from it, and the document's current result is read from the IDP service.

Specification: `specs/006-event-document-result/`. Example event: `sample_events.json` (valid JSON; `tests/tools/make_sample_event.py` repaired it from a Python-style dump).

## Which action runs

One Lambda, and its `action` decides (a non-empty `action` always wins):

| Payload | Runs |
|---|---|
| the IDP event, with no `action` | `document_event` (the default) |
| `{"action": "document_event", "event": <event>}`, or the event with an `action` key added | `document_event` |
| `{"action": "health", ...}` | the connectivity check (**must** be asked for) |
| `{"action": "add_note" / "get_notes", ...}` | unchanged |
| no `action` and not an IDP event (`{}`, `{"dry_run": true}`, a foreign event) | refused `EVENT_INVALID`; the message says to pass `"action": "health"` |
| an unknown `action` | refused `UNKNOWN_ACTION` |

Because the intake lives in the one Lambda, its role can read and write the jobs table, start its own background work and read the IDP, as well as read the credentials secret. An API Gateway request (it has `httpMethod` and `resource`) is never handed to the intake: it goes to its route.

## What a run does

1. **Parse the event** (object or JSON text). Anything that is not a `SUCCEEDED` status change of Step Functions is **ignored** (nothing is contacted). The document ID is `document.id` in the JSON text of `detail.input` (else `detail.output`); no ID, two different IDs, text that is not JSON or a bad ID is **refused** (`EVENT_INVALID`).
2. **Read the document once** through the IDP SDK. Not found, not finished (with its state) and an unavailable IDP are each refused with their own code.
3. **Build the scan result summary**: document ID, state, class, pages, processing issues, every assessed field with its value and confidence, and the fields below their threshold. No page images, raw text or URIs.
4. **Check the document is usable** with the rules of the job-number workflow ([RESERVATIONS.md](RESERVATIONS.md)): it must be finished, hold every field the save needs, and have no needed field below its confidence threshold unless a person reviewed it in IDP. Otherwise: `refused` (`DOCUMENT_INCOMPLETE` / `DOCUMENT_NEEDS_REVIEW`, naming the fields) and **nothing is reserved**.
5. **Live writes must be on** (`LIVE_WRITES=true` with its approval reference). Off: `read_only`, nothing reserved.
6. **Defaults must exist**: `SYTELINE_DEFAULT_WHSE` and `SYTELINE_DEFAULT_ORDTYPE` (the order and received dates are the date of the event). Missing: `refused` (`DEFAULTS_NOT_CONFIGURED`) before anything is reserved.
7. **Once per document**: a reservation that already exists for the document (made by an earlier event **or by the request workflow**) is returned as `already_processed`; a concurrent delivery finds the claim and does the same.
8. **Reserve** one CO number and one job number per line (the customer must be one confident match, as in the request workflow), then **accept the save and start the worker**. The response is `save_accepted` with the numbers; follow the save with the status request of [RESERVATIONS.md](RESERVATIONS.md).

The reservation is recorded as made by `event-intake:<execution name>` (`uploaded_by` `event-intake`); no person confirmed the PO, so the safeguards are the thresholds, the review requirement, the live-writes switch and the once-per-document rule.

## A job number the document already carries (optional)

A line item of the document may carry its own job number. Map it with `IDP_FIELD_MAP`, key `line.job` (the default document field is `job_number`; that name is unconfirmed, like the other defaults), for example `{"line.job": "work_order"}`.

- For a line where it is present and not blank, the reservation **uses that number and does not allocate a new one**. Other lines are allocated as before. The CO number is always allocated.
- The response, the reservation status and the reserve response show, per job, `"source": "document"` or `"allocated"`.
- It is read like any other field the save uses: below its confidence threshold and not reviewed, the document is refused (`DOCUMENT_NEEDS_REVIEW`) and nothing is reserved.
- **A supplied number is not checked**, neither against the ERP nor against the other lines. If the ERP rejects the job (for example the number is already in use), the order ends `needs_reconciliation` after the CO header and earlier lines were written; nothing is retried or removed. Follow [reconciliation-runbook.md](reconciliation-runbook.md). A supplied number whose line was not saved is reported with usage `supplied_not_saved` (this service did not consume it).

### `JOB_NUMBER` in `deploy.env` (test only)

`JOB_NUMBER=TEST-001` (up to 20 letters, digits, `-`, `_`) makes **every line of every reservation** use that job number: no `NextWOSp` call, `"source": "configured"` in the response, and it wins over a job number the document carries. A PO with several lines gets the same number on each line and the ERP rejects the second job, so use it with single-line documents. Leave it empty (the default) outside a test; `deploy.sh` prints a warning while it is set.

### `DEV_TARGET_CO` in `deploy.env` (development mode)

`DEV_TARGET_CO=129999` (1 to 10 digits; replaces the old `CO_NUMBER`) lets you work in the production ERP without creating anything: you create **one test order in the ERP first**, for the customer your test document resolves to, and name it here. Then:

- every reservation uses that order's number and allocates none (no `NextCoSp`); with `JOB_NUMBER` as well no number routine is called at all. The response carries `"development": {"target": "129999"}`;
- every save **overwrites** the order: the header and the lines are updated (a line the order lacks is added; a line it has that the document does not is left alone and listed as `surplus_lines` with a warning); the job is updated when `JOB_NUMBER` is set and that job exists, otherwise created. Nothing is deleted. Line results say `action: updated` or `inserted`; the save reports `co_action: updated`;
- the job tracker never blocks a run: a document that was already processed is processed again (the tracker still records each run, with `dev_target`), so you can send the same document as often as you like;
- before anything is reserved or written the order must exist (`DEV_TARGET_MISSING`) and belong to the document's customer (`DEV_TARGET_CUSTOMER_MISMATCH`, naming both); a value that is not 1 to 10 digits is refused (`DEV_TARGET_INVALID`) rather than ignored;
- every log line, tracker item, response and status of a run names the target; `deploy.sh` prints a warning while it is set.

If the ERP refuses an update part-way, the run ends `needs_reconciliation` as for any partial write. The ERP's acceptance of updates to the order, line and job rows has not been confirmed against the real ERP yet; the first run's message shows any property or key it does not accept. Leave the setting empty (the default) outside development; with it empty nothing in this section applies.

## Outcomes

| `outcome` | Meaning | Anything reserved or written? |
|---|---|---|
| `ignored` | not a finished-processing event. When the payload is not a Step Functions status change at all, the answer lists `received` (its `source` and `detail-type`), `expected` and the payload's `top_level_keys` (keys only, never values) so you can see what was sent | no, nothing contacted |
| `refused` | `errors[0].code` says why (`EVENT_INVALID`, `DOCUMENT_NOT_FOUND`, `DOCUMENT_NOT_READY`, `DOCUMENT_INCOMPLETE`, `DOCUMENT_NEEDS_REVIEW`, `IDP_UNAVAILABLE`, `DEFAULTS_NOT_CONFIGURED`, `SECRET_STORE_UNREACHABLE`, `STORE_UNAVAILABLE`) | no |
| `read_only` | usable result, but live writes are off, the customer was not matched, or the ERP was unreachable before the first number | no number consumed; a later delivery may try again |
| `reserved` | numbers reserved, the save could not be handed to the worker | numbers; save it through the API |
| `save_accepted` | numbers reserved and the worker started | numbers, then the order (by the worker) |
| `already_processed` | the document was handled before; `first_outcome` says how | no |
| `failed` | a reservation failed after a number was consumed (`UNCERTAIN_OUTCOME` etc.) | numbers may be consumed; **never retried automatically** |

The response is discarded by EventBridge (asynchronous invoke, **no retries**); read what happened in the function's log (document ID, outcome, counts, reservation id; never field values) and in the status request. Log lines and the stored `EVTDOC#` record hold identifiers and codes only.

## Switching it on

1. Verify the IDP calls and the unconfirmed ERP/IDP names read-only first ([TESTING.md](TESTING.md) step 5 and the notes of features 004 and 005).
2. Invoke the Lambda with the example event while live writes are off (`cd src && ./invoke.sh ../sample_events.json`; `./invoke.sh --check ../sample_events.json` looks at the file first; the raw command is): `aws lambda invoke --function-name <DirectFunctionName> --payload fileb://sample_events.json out.json` (use `--payload file://…` with `--cli-binary-format raw-in-base64-out` for AWS CLI v2 text payloads). Expect `refused`/`DOCUMENT_NEEDS_REVIEW` if a field is below its threshold, or `read_only` otherwise; the summary is in the response.
3. Set `IDP_STATE_MACHINE_ARN` in `deploy.env` (PLANT ONLY block; the state machine must be in the same account and region as this stack) and redeploy. A rule now sends each `SUCCEEDED` execution of that state machine to the function. Empty it and redeploy to stop.
4. Turn on live writes only in an approved window ([DEPLOYMENT.md](DEPLOYMENT.md)).

## The example document

`159112.pdf` (class `purchase_order`, 3 pages) has `line_items[0].material_code` at 0.55 against a 0.8 threshold, and its review status is "Review Skipped". Under the rule of the request workflow a person must review a low-confidence field in IDP, so an event for it ends **refused: needs review**, writes nothing, and returns the summary. After the field is reviewed in IDP, invoke the same event again (the refusal consumed nothing, so the document is not blocked).

## When the IDP cannot be reached (`IDP_UNAVAILABLE`)

The response then carries `cause` (the exception type only, never its message) and a `hint`:

| `cause` | Usually means |
|---|---|
| `ModuleNotFoundError` / `ImportError` | the IDP SDK, or one of its dependencies, is not in the deployed package. The response and the log line name the missing module (`module`): `idp_sdk` or `idp_common` means set `IDP_REPO=/path/to/accelerator checkout` in `deploy.env` and redeploy; anything else means a dependency of the SDK is missing from `src/requirements-idp.txt` |
| `KeyError` | `IDP_STACK_NAME` (and the other IDP settings) are not set on the function: fill the IDP block of `deploy.env` and redeploy |
| `NoRegionError` | set `IDP_REGION` |
| `ClientError` | AWS refused the call: the role's IDP permissions (CloudFormation describe, S3 read, DynamoDB read and invoking the IDP stack's `LookupFunction`) are only created when `IDP_STACK_NAME`, `IDP_OUTPUT_BUCKET` and `IDP_TRACKING_TABLE` are all set; an `AccessDeniedException ... lambda:InvokeFunction ... LookupFunction` in the log means the stack was deployed before this permission existed: redeploy |
| `EndpointConnectionError`, `ConnectTimeoutError` | the subnets have no private path to CloudFormation, S3 and DynamoDB (VPC endpoints or a NAT route) |

`./invoke.sh --log <payload>` prints the log tail, which has the line `IDP client could not be created: <type>`.

**`kms:Decrypt ... AccessDenied`** (for example `Failed to get metadata: ... GetObject ... kms:Decrypt on resource: arn:...:key/<id>`): the IDP's output bucket (and perhaps its tracking table) is encrypted with a customer-managed KMS key. Put the key ARN from the message into `IDP_KMS_KEY_ARNS` in `deploy.env` (several, comma-separated, no spaces) and redeploy: the function's role then gets `kms:Decrypt` on exactly those keys. The **key policy** must also allow the role (`sytline-connect-DirectFunctionRole-...`) unless it delegates to IAM; if it does not, the IDP's administrator has to add the role to the key policy.

**Easiest:** ask for the message in the response itself: `./invoke.sh --debug ../sample_events.json` (it sends a temporary copy of the payload with `"debug": true`; add `"debug": true` yourself beside the event, or beside `event`, when invoking another way). An IDP failure then also carries `detail`: the SDK's message on one line, shortened to 700 characters. Without `debug` it is never in the response. Otherwise a failed read (`cause` `IDPProcessingError`, `ClientError`, ...) is explained by the log line `IDP read failed: <type>: <the SDK's message, shortened to 700 characters>`; the message is in the function's log only, never in the response. Typical messages: `AccessDeniedException ... <action> ... <resource>` (add that permission to the role, as `lambda:InvokeFunction` on the IDP stack's LookupFunction was added), `Results not found for document` (the document ID is not in the IDP tracking table), a timeout (no network path from the subnets to that service).

## The entry log line

Every invocation (an IDP event, an API request, a direct action, the schedule's sweep, the function's own background actions) writes one
JSON line first, before anything runs, so a payload that is refused or misrouted can still be read from the log:

```json
{"type": "event_entry", "mode": "redacted", "event": {"detail-type": "Step Functions Execution Status Change", "source": "aws.states",
 "detail": {"status": "SUCCEEDED", "input": {"redacted": true, "chars": 11204, "document_id": "159112.pdf"}, "output": {"redacted": true, "chars": 12981}}}}
```

| Setting `LOG_EVENTS` | `mode` | The line holds |
|---|---|---|
| `false` (default) | `redacted` | the event with the values of `input`, `output`, `body` and `text` replaced by `{"redacted": true, "chars": n}` (an IDP event's `input` also shows the document ID); everything else as received |
| `true` | `whole` | the whole event, uncut (no size cap). Only an event too large for one log entry (the line would pass 200,000 bytes; CloudWatch accepts 256 KB) becomes `{"truncated": true, "chars": N, "head": "<start of the event>", "copy": {"bucket": "...", "key": "events/..."}}`; the full event is in that S3 object |

- Header values `Authorization`, `X-Amz-Security-Token`, `Cookie` and `X-Api-Key` are `***` in both modes.
- The example IDP event is about 20,000 characters, so with `LOG_EVENTS=true` the line holds all of it. `LOG_EVENTS_MAX_CHARS` no longer exists.
- **The oversized-event copy.** While `LOG_EVENTS` is `true` the stack has a bucket (`EventCopyBucketName` in the stack outputs). An event that does not fit one log entry is written there as `events/YYYY/MM/DD/<time>-<id>.json` (the same JSON as the line would have held, header credentials still `***`). The bucket is encrypted, closed to the public, refuses non-TLS requests, expires objects after `EVENT_COPY_RETENTION_DAYS` (default 30) and is **retained when the stack is deleted**; the function can only add objects to it. Who may read the copies is for you to grant.
  If the copy cannot be stored (for example the subnets have no route to S3) the line has `"copy_error": "<ExceptionType>"` instead of `copy`, after at most about 8 seconds, and the run is unaffected. Outside a deployment (no `EVENT_COPY_BUCKET`) it says `"copy_error": "not_configured"`.
- With `true` the logs hold document and order content (names, addresses, part numbers) and are readable by whoever can read the function's log group:
  turn it off again when you have finished debugging. The default keeps the promise of this feature that no field value, customer name or address is logged.
- Building the line never changes the run: any problem leaves one `event_entry could not be built: <ExceptionType>` warning.

## Known limits

- A reservation made **before** this feature has no document marker, so an event for that document is not recognised as already handled. An event and a request that start for the same document at the very same moment can both reserve (the request path has no claim).
- A run that died before reserving leaves a claim; it is taken over after 5 minutes.
- Events are delivered at least once, and EventBridge does not guarantee order; the claim makes repeats harmless.
- The rule works only within one account and region.
- The page count and issue count of the summary are read from the status object as `num_pages` and `processing_issue_count` (unconfirmed SDK attribute names; they show `null` if the SDK uses others).
