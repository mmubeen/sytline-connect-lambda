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

## Known limits

- A reservation made **before** this feature has no document marker, so an event for that document is not recognised as already handled. An event and a request that start for the same document at the very same moment can both reserve (the request path has no claim).
- A run that died before reserving leaves a claim; it is taken over after 5 minutes.
- Events are delivered at least once, and EventBridge does not guarantee order; the claim makes repeats harmless.
- The rule works only within one account and region.
- The page count and issue count of the summary are read from the status object as `num_pages` and `processing_issue_count` (unconfirmed SDK attribute names; they show `null` if the SDK uses others).
