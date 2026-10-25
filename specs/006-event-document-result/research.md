# Research: Document Result from a Processing-Complete Event

## R1. How the scan result is read (the document reviewer)
**Decision**: the result is read the way feature 004 already reads it: `IDPClient.document.get_status`, `.document.get_metadata` (class and extracted fields) and `.assessment.get_confidence` (per-field confidence and `meets_threshold`), from `src/syteline/idp.py`. The IDP SDK documentation (`docs/idp-sdk.md` of the accelerator repository) was fetched through the document reviewer: it confirmed the document exists and describes "a clean, namespaced API" for the accelerator's capabilities, but the reviewer returns only a short preview of the 2455-line file, so it could not confirm individual method signatures. They are confirmed by the working calls already in `idp.py` and are re-checked read-only against the real stack before live use (docs/EVENT_INTAKE.md step 1).
**Alternatives**: using the event's embedded `input`/`output` document as the result (rejected: a snapshot taken mid-processing, `RUNNING`/`EVALUATING`, without extracted field values); reading the result JSON from S3 directly (rejected: bypasses the SDK that features 004 uses).

## R2. What the event is and how the document ID is found
**Decision**: the event is EventBridge's "Step Functions Execution Status Change" (`source` `aws.states`). `detail.input` and `detail.output` are JSON **text** holding `{"document": {"id": ...}}`. `eventdoc.parse(event)` accepts the event as an object or JSON text, ignores it unless `source`/`detail-type` match and `detail.status` is `SUCCEEDED`, parses `input` (then `output`) as JSON when they are strings (or uses them as objects), reads `document.id`, refuses when the two IDs differ, when neither exists, or when the text is not JSON, and checks the ID is 1 to 256 characters of letters, digits, `. _ - /` (no spaces, no `..`). It also returns the execution name/ARN (the caller identity) and the event date (`time`, first 10 characters when `YYYY-MM-DD`, else today UTC). Nothing else from the event is used.
**Alternatives**: trusting `detail.output.document.status` (rejected: it is `EVALUATING` in the example).

## R3. The sample file is not valid JSON
**Decision**: `sample_events.json` as saved is a Python-style dump (single quotes, `None`, spaces inside ARNs and times, raw line breaks inside quoted text). `tests/tools/make_sample_event.py` repairs it into `tests/fixtures/sample_event.json`: it normalises the outer dump, then parses the two embedded JSON texts (`\'` is the only invalid escape in them) and re-serialises `input` and `output` as JSON strings. The saved root file is replaced by the repaired event (same name) so invoking the function with it works; `docs/sample_events.json` stays deleted.
**Alternatives**: making the parser tolerant of Python-style dumps (rejected: real events are valid JSON; tolerance would hide real faults).

## R4. Where it runs and how it is started
**Superseded in part by R11**: the intake is not a separate function; everything below about the function (VPC, policies, timeout, no retries) applies to `DirectFunction`, and the rule and the `IdpStateMachineArn` parameter are unchanged.
**Decision**: a new `EventIntakeFunction` in `src/template.yaml` (plant, in the host VPC, same security group and subnets as `ReserveFunction`, same ERP/database/IDP environment, plus `WORKER_FUNCTION_NAME`, `SYTELINE_DEFAULT_WHSE`, `SYTELINE_DEFAULT_ORDTYPE`; policies: the jobs table, the secret, IDP read, invoke of the worker). Timeout 60 s. A new optional parameter `IdpStateMachineArn` (pattern `^$|^arn:aws[a-z-]*:states:...:stateMachine:...$`): when given, an `AWS::Events::Rule` matching `source aws.states`, `detail-type` "Step Functions Execution Status Change", `detail.status` `SUCCEEDED` and `detail.stateMachineArn` equal to it targets the function; when empty no rule exists (the function can still be invoked directly with an event). The function's async invoke config has **MaximumRetryAttempts 0** (a replayed run must not be automatic).
**Rationale**: a deliberate act (supplying the ARN) turns automatic processing on; live writes stay off by default anyway. Same account and region only (EventBridge).
**Alternatives**: a direct EventBridge rule on all `aws.states` events (rejected: every execution of every state machine in the account); having the IDP stack call the function (rejected: changes the IDP stack).

## R5. Once per document, and the outcome record
**Decision**: before any work beyond reading, `eventstore.claim(document_id, execution)` does a conditional put of `EVTDOC#<document_id>` (`state: processing`). If the claim fails the item is read and its recorded outcome returned (the scan summary is still produced). Outcomes: `reserved`, `save_accepted` (with `reservation_id`), or a refusal. A refusal that nothing was reserved for (needs review, not ready, document not found, IDP unavailable, live writes off, customer not matched, defaults missing, ERP unreachable before any number) **releases** the claim (delete) after recording `last_outcome`, so a later delivery or a manual re-invoke can proceed once the cause is fixed (for example after a review in IDP). Once a CO or job number has been reserved the claim is permanent. Before claiming, `eventstore.existing_reservation(document_id)` reads the `DOCRES#<document_id>` marker; if a reservation exists (made by the request workflow or by an earlier event) the function returns it as the first outcome and does nothing else.
`reservations.create_reservation` writes `DOCRES#<document_id>` -> `reservation_id` (unconditional put, last wins) for every reservation, so the marker exists for both paths from this feature on. Known limit: reservations made before this feature have no marker, and an event and a request that start at the very same instant for the same document can both reserve (the request path has no claim); the window is the reservation's duration.
`request_ref` for the reservation is `event-<document_id>` (at most 128 characters), so even without the marker a repeat of the event path cannot reserve twice (`create_reservation` is idempotent on it).
**Alternatives**: one item for both purposes (rejected: the marker must also be written by the API path); DynamoDB streams or a queue (rejected: more moving parts for tens of documents a day).

## R6. Reserve, accept the save, start the worker
**Decision**: after one `idp.read_document()` the handler builds the summary and, when `po_details` accepts the bundle, calls `run_reserve({"request_ref": "event-<id>", "document_id": id, "uploaded_by": "event-intake"}, soap, store, caller_identity="event-intake:<execution name>", source=lambda _id: doc)` (the existing `source` argument avoids a second IDP read). On a `reserved` reservation it calls `savestart.accept_and_start(store, lambda_client, rid, params)` with `params = {document_id, order_date, received_date, warehouse, order_type}` from `config.event_defaults(event_date)`; this function is extracted from `handlers/save_intake.py` and used by both. A reservation that failed is reported and never retried or saved (feature 004 rules). The save runs in the existing worker, asynchronously; the response says `save_accepted` and the reservation id, and the status request of feature 004 reports the rest.
**Alternatives**: running the save inline (rejected: the worker holds the claim/park logic and its own time budget).

## R7. Defaults for the values the document does not hold
**Decision**: `config.event_defaults(event_date)` returns `order_date = received_date = event_date`, `warehouse = SYTELINE_DEFAULT_WHSE`, `order_type = SYTELINE_DEFAULT_ORDTYPE`; when a warehouse or order type is blank it returns the missing names and the handler refuses with `DEFAULTS_NOT_CONFIGURED` **before** reserving. The PO date is not read from the document: the example's order date field name is unconfirmed (feature 004 notes), so the event date stands in, as the spec assumes.

## R8. The response, the log and the record
**Decision**: the handler returns `{"outcome": ..., "document_id": ..., "summary": {...}, ...}` (contracts/event-intake.md); for EventBridge's asynchronous invoke the response is discarded, so the outcome is also written to the `EVTDOC#` item (document ID, execution, outcome code, reservation id, timestamps; no field values, no PO details). The summary lists each extracted field's **path, value and confidence** (values are the requested "scan results" and are in the response only, never logged or stored), the below-threshold paths, class, state, page count and issue count; no page images, raw text or URIs. Log lines carry the document ID, event status, outcome, counts and reservation id only.

## R9. Error and outcome codes
**Decision**: new `EVENT_IGNORED` (not an error: outcome `ignored` with a reason), `EVENT_INVALID` (no or conflicting document ID, bad JSON, bad ID), `DEFAULTS_NOT_CONFIGURED`; existing codes are reused for the rest (`DOCUMENT_NOT_FOUND`, `DOCUMENT_NOT_READY`, `DOCUMENT_INCOMPLETE`, `DOCUMENT_NEEDS_REVIEW`, `IDP_UNAVAILABLE`, `LIVE_WRITES_DISABLED`, `CUSTOMER_*`, `ERP_UNREACHABLE`, `UNCERTAIN_OUTCOME`).

## R10. The example document
**Decision**: the example's `confidence_threshold_alerts` lists `line_items[0].material_code` (0.55 against 0.8) and `hitl_status` is "Review Skipped", `hitl_triggered` true. `idp._low_confidence` uses the assessment's `meets_threshold`, so the document is refused with `DOCUMENT_NEEDS_REVIEW` naming the field unless a person reviews it in IDP; the event run therefore ends `refused: needs review` with the summary and writes nothing. Whether "Review Skipped" should count as reviewed is not changed here (it would change feature 004's rule); the quickstart states the expected result.

## Implementation notes (2026-10-22)
- **request_ref**: `event-<sha256(document_id)[:16]>-<random 12 hex>`, a new one for every attempt (R5/R6 said `event-<document_id>`). A failed reservation that consumed no number must not block a later attempt, and an idempotent `request_ref` would return that failed reservation forever. Once-per-document is enforced by the `DOCRES#` marker (a reservation that is not failed-without-numbers is final) and the `EVTDOC#` claim.
- **Stale claim**: a claim older than 300 seconds with no reservation (the run died) is taken over once (`eventstore.STALE_AFTER`).
- **Summary counts**: pages and issues are read from the status object as `num_pages` and `processing_issue_count` (unconfirmed SDK names; `null` when absent).
- **Database function lists** (superseded by R11, `DirectFunction` already carried them): `EventIntakeFunction` joins reserve, the worker and the connectivity check as a function that carries the database variables (tests updated).

## Revision 2026-10-22: one Lambda, `action` decides (R11 to R16)

## R11. The Lambda that is consolidated
**Decision**: the intake becomes an action of `lambda_function.lambda_handler`, the handler of the plant `DirectFunction` (which already has the ERP, database and secret settings, the plant VPC and a 60 s timeout). `EventIntakeFunction` is removed from `src/template.yaml` and `src/Makefile`. `DirectFunction` gains what the intake needs and it lacks: the IDP environment (`IDP_STACK_NAME`, `IDP_REGION`, `IDP_DOCUMENT_CLASS`, `IDP_FIELD_MAP`), `WORKER_FUNCTION_NAME`, `SYTELINE_DEFAULT_WHSE` / `SYTELINE_DEFAULT_ORDTYPE`, the jobs-table CRUD policy, `LambdaInvokePolicy` on the worker, and the IDP read policy (the same conditional statement as `ReserveFunction`); `EventInvokeConfig.MaximumRetryAttempts: 0` (it affects asynchronous invokes only, so EventBridge and `--invocation-type Event` do not retry while ordinary synchronous invokes are unchanged). The rule and permission now target `DirectFunction`. `DirectFunctionName` is the single output (the `EventIntakeFunctionName` output is dropped).
**Rationale**: spec FR-011 and FR-015; one place to invoke, one set of permissions to review.
**Consequence**: the direct-invoke role can now read and write the jobs table, start the worker and read the IDP: that is the union of the intake's needs and is stated in docs/EVENT_INTAKE.md. Tests that list the functions carrying the database variables return to the original three.

## R12. Action resolution
**Decision**: `lambda_handler` resolves the action in this order: (1) an explicit non-empty `action` wins (`health`, `add_note`, `get_notes`, `document_event`; anything else is refused with `UNKNOWN_ACTION` as today); (2) otherwise the action is `document_event`. The payload of `document_event` is the event itself, or the value of an `event` key (object or JSON text); an `action` key next to the event fields is ignored by the parser. `handlers/event_intake.py` exposes `process(payload, context)` (the former `handler` body); the module keeps no Lambda entry point.
**Alternatives**: an EventBridge input transformer adding `action` (rejected: the clarification made the intake the default, so the raw event works unchanged).

## R13. The strict refusal
**Decision**: when the action was **defaulted** (no `action`) and the unwrapped payload is not an IDP status change event (`eventdoc.is_status_change`: `source` `aws.states` and the Step Functions detail type), `lambda_handler` returns `failure("document_event", EVENT_INVALID "no action given and the payload is not an IDP processing event; to run the health check pass {"action": "health"}")` and contacts nothing. With an **explicit** `document_event`, a foreign event is `ignored` as before (FR-013 applies to a missing action only). A status-change event with a status other than `SUCCEEDED` is `ignored` either way.

## R15. The response shape and log
**Decision**: `document_event` returns the outcome object of contracts/event-intake.md plus `"action": "document_event"` and `"ok"`, true for `ignored`, `already_processed`, `reserved` and `save_accepted`, false otherwise (so the shared per-call log line and callers that read `ok` behave like the other actions). The per-call log keeps `record: null` for it; the intake's own log line stays.

## R16. Callers that relied on the default health action
**Decision**: every caller that omitted `action` for the health check is changed to pass `"action": "health"`: `tests/contract/test_health.py` (3 calls), `tests/unit/test_transport.py` (where the event has no action), `tests/unit/test_secret_store.py`, the `__main__` local run in `lambda_function.py`, `README.md` ("omit `--payload` for the default `health` action"), and any doc example of a bare `lambda invoke` for the health check. New tests assert the old bare forms now return the strict refusal. `docs/DEPLOYMENT.md` and `docs/TESTING.md` are searched for bare health invocations and updated.

## R17. Where and what (FR-016)
**Decision**: the first statement of `lambda_handler`, before routing, calls `eventlog.log_entry(event)`. It writes one line `{"type": "event_entry", "mode": "redacted"|"whole", "event": ...}` through the root logger at INFO,
the same logger and shape family as the other lines (a `type` field, one JSON object per line).
**Rationale**: before routing means every kind of invocation is covered (IDP event, API request, direct action, sweep, the function's own `work_*` actions); the `type` field keeps the existing log readers and `test_logging` happy (the removed ad-hoc line had none).
**Alternatives**: logging inside each handler (misses the unrouted and refused payloads, the ones that most need it); the platform's own request logging (does not exist for Lambda event payloads).

## R18. Redaction (the default)
**Decision**: a recursive copy of the event in which the value of any key in {`input`, `output`, `body`, `text`} is replaced by `{"redacted": true, "chars": n}` (`n` = its length, as text if it is a string, as JSON if not). For an IDP event the replacement of `detail.input` also carries `document_id` (read with the same parser the intake uses; a failure to read it just omits it).
Everything else is kept: `source`, `detail-type`, `status`, ARNs, names, times, `httpMethod`, `resource`, `pathParameters`, `action`, `record_type`, `record_key`, `reservation_id`.
**Rationale**: those four keys are exactly where document text (`input`/`output` hold page classification text, as the example event shows), order bodies (`body`) and note text (`text`, the `add_note` action) arrive; the rest are identifiers needed to debug routing.
**Open edge**: query-string values and other action fields are kept (they are identifiers today); a new action carrying free text must add its key to the set (a test lists the set).

## R19. Opt-in whole event and the credentials rule
**Decision**: `LOG_EVENTS=true` logs the whole event, except that header values named `Authorization`, `X-Amz-Security-Token`, `Cookie` and `X-Api-Key` (any case) are always masked as `***`, in both modes. [Superseded by R27: there is no cap any more.] Over the cap (default 8000 characters, `LOG_EVENTS_MAX_CHARS` to change, not exposed in the template) the line carried `{"truncated": true, "chars": N, "head": "<first cap characters>"}` instead of the event so every line stays one valid JSON object under CloudWatch's limit.
**Rationale**: the project's standing rule (credentials and secrets are never logged) outranks the opt-in; a signed request's `Authorization` header is a credential-equivalent.

## R20. It must never change the run
**Decision**: `log_entry` catches every exception (non-serialisable values use `default=str`; bytes become a size note; `None` and other non-dict values log their type and size; a string that is valid JSON is parsed and redacted like an object), logs one short warning `event_entry could not be built: <ExceptionType>` and returns. It is not wrapped around the handler's own work.
**Alternatives**: letting it raise (would turn a logging problem into a failed ERP event).

## R21. The setting
**Decision**: template parameter `LogEvents` (`"false"` default, allowed `"true"`/`"false"`) -> function environment `LOG_EVENTS`; `deploy.sh` reads optional `LOG_EVENTS` from `deploy.env` (default `false`) and always passes it, like the other optional settings; the deploy notice warns when it is `true` that logs will hold document and order content.
**Rationale**: one switch, visible in the deployment settings, off unless chosen; no new permission. The mock environment (feature 008) picks it up through the template rendering with no change.

## R22. Where the supplied number is read
**Decision**: a new optional line key `line.job` in the field map (`idp_fields.DEFAULT_MAP`, default document field `job_number`, UNCONFIRMED like the other defaults; override with `IDP_FIELD_MAP` `{"line.job": "<field>"}`). It is read as text (whitespace collapsed, as for `line.item`); a value that is absent, empty or only blanks means "not supplied". It is never required.
**Rationale**: the clarified source is the document, per line (spec session 2026-10-25); reusing the mapping means no new setting and no code change when the real field name is known.
**Alternatives**: a caller-supplied list in the reserve request (rejected in clarification; the event path has no caller); a header-level job number (a job belongs to one line).

## R23. Reserve: use, do not allocate
**Decision**: in `run_reserve`, for each line, if the line has `job` that value goes into the entry and `allocate("next_job", ...)` is not called; otherwise allocation is unchanged. The CO number is always allocated. The live-writes gate stays before the CO allocation, so it still guards every reservation. Supplied numbers are not added to `consumed` (the unused-numbers report in a failure lists only numbers this service consumed).
**Rationale**: FR-006e/FR-023: no new job number when the value is set. A reservation with every job number supplied still reserves a CO number, which is a write, so the gate is kept.
**Alternatives**: skipping the whole reservation when all numbers are supplied (the CO number is still needed).

## R24. No check of the supplied number
**Decision**: none, per the clarification: no read against the ERP, no comparison between lines. The save inserts the job with that number; an ERP rejection goes through the existing path (`pipeline._insert`/`savewf._insert`: a rejection after earlier rows were written parks the order as `needs_reconciliation`; never retried or replayed). The only check is the existing one on the whole field: a value over its confidence threshold unreviewed is refused (R25).
**Rationale**: the owner's choice (option C). The cost is a possible partly written order, which the existing reconciliation runbook covers; it is stated in the spec, the quickstart and `docs/EVENT_INTAKE.md`.
**Alternatives**: read-only existence check before reserving, refusing the document or only the line (options A, B; rejected).

## R25. Confidence and the fingerprint
**Decision**: the job field path is added to `used` in `map_fields`, so the existing confidence check covers it (a supplied number read below its threshold is `DOCUMENT_NEEDS_REVIEW` until a person has reviewed it). The reservation fingerprint gains the supplied numbers (`line=job` pairs) only when at least one is supplied, so a reservation without any keeps the fingerprint it had, and save refuses if the document's supplied numbers changed since the reserve.
**Rationale**: an OCR-misread job number is the most damaging wrong value here, since it names an ERP record; binding it to the reservation keeps reserve and save on the same numbers.
**Alternatives**: exempting the field from the confidence check (rejected: with no ERP check it is the only safeguard); always hashing the field (would invalidate in-flight reservations).

## R26. Reporting
**Decision**: each entry of `job_numbers` gets `"source": "document" | "allocated"` (entries stored before this change are read as `allocated`). `numbers_report` copies it to every `jobs[]` item (reserve, status, event outcome) and, for a supplied number whose line was not saved, reports `usage: supplied_not_saved` instead of `consumed_unused`.
**Rationale**: FR-006e requires the response to show per line which it was; the old usage value would claim this service consumed a number it did not.

## R27. No cap; the one limit is the log service's
**Decision**: remove the 8000-character cap and `LOG_EVENTS_MAX_CHARS`. With `LOG_EVENTS=true` the line holds the whole event (header credentials masked as before). The only limit is what one log entry can hold: CloudWatch accepts 256 KB per entry, so the code uses `MAX_LINE_BYTES = 200,000` (UTF-8 bytes of the finished line, leaving room for the platform's own prefix and for JSON escaping).
**Rationale**: the clarified requirement (session 2026-10-25, "log the raw event"): no truncation in the normal case. Typical events are tens of KB (the example is about 20 KB), far below the limit.
**Alternatives**: keeping a configurable cap (rejected: it is the behaviour being removed); splitting into numbered lines (offered, not chosen).

## R28. An event over the line limit: a head in the log, the whole event in S3
**Decision**: if the finished line is over `MAX_LINE_BYTES`, the line becomes `{"type": "event_entry", "mode": "whole", "event": {"truncated": true, "chars": N, "head": "<the start of the event JSON, cut by halving until the line fits>", "copy": {"bucket": "...", "key": "..."}}}`. The copy is the same masked JSON text, written with `put_object` (key `events/YYYY/MM/DD/<HHMMSS>-<uuid4>.json`, `ContentType` application/json, `ServerSideEncryption` AES256). It is tried only in `whole` mode (a redacted line is always small) and only when the bucket setting is present. If the write fails, `copy_error` holds the exception type; if there is no bucket (a run outside the deployment), `copy_error` is `not_configured`. The client uses short timeouts (connect 3 s, read 5 s, no retries) so a missing route adds at most about 8 s, once.
**Rationale**: the clarified answer (S3 copy with the location in the line) while keeping the rule that logging never changes the run (R20): every failure is swallowed and shown in the line.
**Alternatives**: a second Lambda or queue for the copy (more parts, no benefit); writing every whole event to S3 (needless data at rest; the line suffices below the limit).

## R29. The bucket (a supporting resource)
**Decision**: `EventCopyBucket`, `AWS::S3::Bucket`, created only when `LogEvents` is `"true"` (condition `LogsEventsToS3`); no fixed name; `DeletionPolicy` and `UpdateReplacePolicy` Retain (as for the jobs table); server-side encryption SSE-S3 (AES256); all four public-access-block settings on; a lifecycle rule expiring objects after `EventCopyRetentionDays` (Number parameter, default 30, 1 to 365); no versioning (so expiry really removes the data); a bucket policy denying requests without TLS; an Output with the bucket name. The function's role gets `s3:PutObject` on `<bucket>/events/*` only, under the same condition (no read, list or delete).
**Rationale**: the clarified answer (the stack creates it); feature 007 FR-003 allows supporting non-network resources. The copy holds customer and order data, so it is encrypted, closed to the public, short-lived and write-only for the function. Retain protects the data from a stack deletion surprise, as the jobs table is.
**Consequences** (documented): setting `LogEvents` back to `false` removes the bucket from the stack but keeps the bucket and its objects until they expire; setting it to `true` again creates a new bucket. Reading the copies needs permissions the deployer grants separately.
**Alternatives**: SSE-KMS with a stack key (adds a key policy to maintain; SSE-S3 is enough here); a fixed bucket name (collides across stacks and accounts).

## R30. The network path to S3
**Decision**: the function runs in the existing VPC, so the subnets need a route to S3 (a gateway endpoint or the existing outbound path). It is documented as an optional requirement of `LOG_EVENTS=true`; without it the copy fails quickly (R28) and the line says so. No endpoint is created (feature 007: no network resources).
**Rationale**: the stack cannot create network resources; failing visibly and harmlessly is the safe behaviour.

## R31. The settings
**Decision**: `deploy.env` gains optional `EVENT_COPY_RETENTION_DAYS` (default 30, integer 1 to 365, validated by `deploy.sh`, always passed as `EventCopyRetentionDays`). The `LOG_EVENTS=true` notice also says that a retained bucket holding full event copies exists and when they expire. The function reads `EVENT_COPY_BUCKET` (set by the template to the bucket name when the condition holds, else empty).
**Rationale**: one switch (`LOG_EVENTS`) still decides everything; the retention is the only new knob.

## R32. The mock environment
**Decision**: the mock renders the template with `LogEvents` true when asked; `render_env` maps `EventCopyBucket` to a valid lowercase name and the S3 statement is written with `Fn::Sub` on the bucket reference (not `GetAtt`, which the mock cannot resolve); the edge seeds the bucket in Moto when `EVENT_COPY_BUCKET` is set, so an oversized event can be exercised end to end.
