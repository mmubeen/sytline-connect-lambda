# Quickstart: validating the event intake

## Prerequisites
Features 004 and 005 working in the plant deployment (see `docs/TESTING.md`); live writes OFF for steps 1 to 5.

## 1. Read-only check of the IDP calls (before anything is deployed)
Run the IDP calls of `docs/TESTING.md` step 5 for `159112.pdf` (status, metadata, confidence) and note the class and the field paths. Confirms the SDK usage of research.md R1.

## 2. Repair the example event
`python tests/tools/make_sample_event.py` writes `tests/fixtures/sample_event.json` and the repaired `sample_events.json`; both parse as JSON and give document ID `159112.pdf`.

## 3. Unit run (no AWS)
`PYENV_VERSION=3.12 pytest tests/unit/test_eventdoc.py tests/unit/test_event_intake.py tests/unit/test_eventstore.py tests/unit/test_idp_summary.py tests/unit/test_template_event.py`.

## 4. Direct invoke with the example event (live writes off)
`aws lambda invoke --function-name <DirectFunctionName> --payload fileb://sample_events.json out.json` (no `action`: the intake is the default): `outcome` is `refused` with `DOCUMENT_NEEDS_REVIEW` naming `line_items[0].material_code`, the `summary` shows class `purchase_order`, 3 pages and that field below threshold; no reservation exists (check with the status request of feature 004).

## 5. Ignored events
Invoke with the same event with `detail.status` set to `FAILED`, then with `source` changed, then with `input` and `output` document IDs different: `ignored`, `ignored`, `refused EVENT_INVALID`; the function log shows no IDP call.

## 6. Live run (only in an approved test window)
With `LIVE_WRITES=true`, the approval reference, the unconfirmed names verified, defaults set, and a document with no field below threshold (or one reviewed in IDP): invoke the event; `outcome` `save_accepted`, one CO and one job number per line; invoke it again: `already_processed` with the first outcome; the status request shows the save progress.

## 7. Automatic start
Set `IDP_STATE_MACHINE_ARN` in `deploy.env`, redeploy, upload a document to the IDP input bucket: one invocation after the execution succeeds (CloudWatch), same outcomes as step 4 or 6.

## 8. One Lambda, `action` decides (revision)
- `{"action": "health"}` runs the connectivity check as before; `{}` and `{"dry_run": true}` are refused `EVENT_INVALID` and the message says to pass `"action": "health"`.
- `{"action": "document_event", "event": <the sample>}` and the raw sample give the same outcome; an unknown action gives `UNKNOWN_ACTION`.

## Entry log (FR-016)

1. Default: invoke any payload (`./invoke.sh ../sample_events.json`) and find the line with `"type": "event_entry"` in the function's log: `mode` is `redacted`, `detail.input` and `detail.output` show only sizes and the document ID, and no field value from the document appears anywhere in the log.
2. Opt in: set `LOG_EVENTS=true` in `deploy.env`, redeploy, invoke again: `mode` is `whole` and the line holds the event (header credentials still `***`); a payload larger than 8000 characters shows `truncated`.
3. Odd input: invoke with a plain string or `null`: the run behaves as before and the line says what arrived.
4. Turn it off again (`LOG_EVENTS=false`) when finished: it exposes document and order content to whoever can read the logs.

