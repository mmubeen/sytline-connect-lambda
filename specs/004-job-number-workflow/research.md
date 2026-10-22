# Research: Job Number Reservation and Save Workflow

## R1. What "get the job number" means in the demo
**Decision**: reserve the next free job number from SyteLine; never look one up. In the demo (`JOB_NUMBER_RETRIEVAL.md`, `runner.py`) `NextWOSp` is called once per PO line before any line is saved; the number is the link between the CO line (`ref_num`) and the job (`Job`, `RootJob`). A number is consumed even if a later save fails.
**Rationale**: matches the spec's assumption and SyteLine's own behavior.
**Alternatives**: look up the job number of an already-saved order (out of scope, would be a read-only `get` on the job collection).

## R2. How to obtain numbers without SQL
**Decision**: call the same web-service methods feature 001 already uses (`NextCoSp`, `NextWOSp` through `idos.next_co()` / `idos.next_job()`, gated by live writes). The demo's SQL stored-procedure call is not used (FR-015, `tests/unit/test_no_sql.py`).
**Rationale**: the routines are SyteLine's own, so numbers never collide across callers (SC-002); no database credential exists.
**Open (UNCONFIRMED, discovery task)**: which IDO exposes each method; already overridable by environment variable (`idos.py`).

## R3. Reserve is synchronous, save is asynchronous
**Decision**: `POST /job-reservations` runs in a VPC function and returns the numbers; `POST /job-reservations/{id}/save` records the request and hands it to the existing worker, like `POST /confirmed-orders`.
**Rationale**: the caller needs the numbers immediately (to name filed copies, for example); the save can take minutes for many lines and must not hold a request open.
**Alternatives**: both asynchronous (extra polling for numbers); both synchronous (save could exceed the API's 29 s limit).

## R4. Fitting reserve in the 29 s API limit
**Decision**: maximum 25 lines per reservation (refused with `TOO_MANY_LINES` beyond it) and the function timeout is 28 s; before each allocation the run checks remaining time (the same `MIN_REMAINING_MS` guard as the upload). If time runs out after some numbers were obtained, the reservation ends `failed` with those numbers listed as consumed and unused; nothing is silently continued.
**Rationale**: roughly 1 customer lookup + 1 CO + N job calls; at under a second each 25 lines fits with margin, and SC-001 asks for 20 lines in under 30 s.
**Alternatives**: raising the cap and going asynchronous for large POs (not needed for current volumes).

## R5. Recording reservations safely
**Decision**: create the reservation item (state `reserving`) and its `RESREF#<request_ref>` index item atomically before the first ERP number is requested, then append each number to the item as it is obtained, and finish as `reserved`. A repeat of the same `request_ref` with the same payload returns the existing reservation (no new numbers); same ref with a different payload is a conflict. A `reserving` item that is never finished (the function died) is read as `failed` with the numbers recorded so far consumed; it is never resumed or silently re-reserved.
**Rationale**: a retry after a lost response must not burn another set of numbers; numbers obtained before a crash stay accounted for (FR-011).
**Alternatives**: write the item only at the end (a crash would lose track of consumed numbers).

## R6. Single use, PO binding, ownership
**Decision**: the reservation stores a fingerprint (SHA-256 of normalized customer name and address fields, PO number and line count) and the caller identity. Save recomputes the fingerprint from the supplied PO details and requires the same caller and `uploaded_by`; the claim `reserved -> save_accepted` is a conditional update, so only one save can ever start. Anything else is refused before any ERP access: `RESERVATION_MISMATCH`, `RESERVATION_FORBIDDEN`, `RESERVATION_USED`, `RESERVATION_NOT_READY` (still `reserving`, or failed).
**Rationale**: FR-009. The customer number is taken from the reservation (resolved once, at reserve time); the caller can never supply it.
**Caller identity**: the API's `userArn`; for an assumed role the session name is dropped, so the same role matches across sessions.
**Alternatives**: re-resolving the customer at save (extra ERP calls, risk of a different answer between the two steps).

## R7. Per-line outcome and stopping rule
**Decision**: save follows the demo's per-line order (CO line with `ref_num` = job number, teardown date, job), but keeps feature 001's safety rule: stop at the first problem. After a definite ERP rejection the run reads back the rows of that line; if they are proven absent the reservation ends `partly_saved` (earlier lines are individually verified, later lines are `not_attempted`); if absence cannot be proven, or the outcome is uncertain (timeout), it ends `needs_reconciliation`. Nothing is deleted and nothing is retried or replayed.
**Rationale**: the demo carried on after a failed line, but doing so after an uncertain write could create a second set of rows; the spec forbids replay (FR-010). Per-line results make the partial state readable (FR-008, FR-012).
**Alternatives**: continue with the next line after any failure (risks writes after an uncertain outcome).

## R8. Teardown date as a separate step
**Decision**: after the line insert succeeds, set the teardown date with an update call; a failure is recorded as a `warning` on that line and the job insert still runs (FR-007, as in the demo). A client `update` operation is added, gated by live writes and classified like `insert` (rejected, unreachable, uncertain). An uncertain outcome on this step is still treated as uncertain for the whole run (stop, `needs_reconciliation`), because a transport failure after sending cannot be shown to be harmless; only a definite rejection is a warning.
**UNCONFIRMED**: the update payload and the teardown property (feature 001 assumed property `Teardown` on `CCT_Coitems`); verify with the read-only discovery task before the first live run.

## R9. Header and line fields from the demo
**Decision**: the CO header also carries the customer PO number and received date (demo: `cust_po`, `date_received`); the job carries engine and serial details and packing if supplied (demo: `engine_sn`, `serial_num`, `packing`). The save request accepts them as optional fields; all property names go in `idos.py` marked UNCONFIRMED.
**Out of scope**: customer default terms and ship-via from the demo's `get_customer_defaults` (feature 001 does not set them); can be added after discovery if the plant wants them.

## R10. Where it runs
**Decision**: VPC version only. The stand-alone version excludes the jobs table and the worker (feature 003, FR-004), and reservations need durable state.
**Alternatives**: a direct-invoke `reserve` action in the stand-alone version (no way to enforce single use without a store).

## R11. Sweeper
**Decision**: the sweeper re-drives `save_accepted` reservations older than 120 s (no write can have happened yet) and parks `saving` reservations older than the worker timeout plus 30 s as `needs_reconciliation` (`STALE_RUNNING`), exactly like uploads. `reserved` items never expire; unused ones are reported by the status request.


## Revision 2026-10-21: document source (R12 to R17)

## R12. Where the PO details come from
**Decision**: the IDP solution's inference result, read with the IDP SDK (`IDPClient(stack_name, region).document.get_metadata(document_id, section_id=1)`: `fields`, `document_class`, `confidence`; `document.get_status(document_id)`: processing state; `assessment.get_confidence(document_id)`: per-field `confidence`, `confidence_threshold`, `meets_threshold`). The document ID is the S3 key `batch-id/filename`. Reserve and save both take it; neither accepts customer, PO number or lines. The reservation item no longer stores the save request or the PO number, only `document_id`, the line count, the fingerprint and the results.
**Rationale**: the data already exists in the IDP stack; sending and storing a copy duplicates it and lets the two drift apart (FR-018, FR-021).
**Note**: the SDK's `get_metadata` reads the section's `result.json` from the IDP output bucket (and resolves the stack's resources through CloudFormation), not our table.

## R13. When a result may be used
**Decision**: three checks, in order, before anything is reserved or written (FR-019): (1) the document exists and `get_status` says it finished (`COMPLETED`; `HITL_IN_PROGRESS` and the rest are refused with the state named); (2) every field the save needs is present in the mapped result; (3) every needed field meets its threshold (`assessment.get_confidence`); a field below its threshold is refused as `DOCUMENT_NEEDS_REVIEW` naming the fields, until review is recognised (below).
**OPEN, to verify against the IDP solution**: how a human-reviewed result is recognised through the SDK. The SDK exposes no review operation; the HITL doc says the final review resolution updates tracking and re-triggers downstream steps, and a document waiting for review has state `HITL_IN_PROGRESS`. The plan assumes: `COMPLETED` after review means reviewed, and `get_metadata`/`get_confidence` then return the corrected values. If a reviewed field still reports `meets_threshold = false`, the rule needs a different signal (a review flag in the tracking record); do not enable live writes until checked.

## R14. Field mapping
**Decision**: a mapping table in one place (`src/syteline/idp_fields.py`, names UNCONFIRMED like `idos.py`): the expected document class, the path of each header field (customer name, address lines, city, state, postal code, PO number, received date, order date) and of the line list with each line's item, quantity, unit of measure, due date, price, teardown, engine serial, serial number and packing. Paths are dotted names into `fields`; list items use the list's own key. The mapper normalises values (strings trimmed, quantity and price to numbers, dates to YYYY-MM-DD) and reports every missing or unreadable required field together.
**Pending**: the class and field names from the owner of the IDP configuration.

## R15. No PO data in our table, and no drift between reserve and save
**Decision**: reserve fetches and maps the result, resolves the customer, and stores `document_id`, the fingerprint and the resolved customer match. Save intake fetches and maps again and compares fingerprints; the worker fetches a third time when it runs and compares again, then writes. A difference at either point refuses the save (`DOCUMENT_CHANGED`) before any write; at the worker it ends `failed` with nothing written. The fetched details live only in memory.
**Rationale**: FR-020 and FR-021. The worker is the only thing that writes, so it must use the current result, and a re-review between reserve and save is caught.
**Alternatives**: freeze a copy at reserve (stores PO data, the thing to avoid).

## R16. Running the SDK inside the Lambda
**Decision**: package `idp_sdk` with the functions that need it, behind one thin wrapper (`src/syteline/idp.py`, `IdpSource`) that the rest of the code and the tests use, so the SDK is imported in one place only. Configuration: `IDP_STACK_NAME`, `IDP_REGION`. Permissions the SDK's calls need: read the IDP output bucket, read the IDP tracking table, describe the IDP stack (to find the buckets); across accounts, a role the functions assume.
**Risks**: (a) `idp_sdk` requires `idp_common`, which is large; the Lambda layer limit may force a trimmed install. Fallback: read `<document_id>/sections/1/result.json` and the tracking record directly with boto3, the same layout the SDK uses, behind the same `IdpSource` interface. (b) The functions run in the plant-routed VPC: they need a private path to S3 (gateway endpoint), DynamoDB (already there) and CloudFormation (interface endpoint), or a NAT route. (c) GovCloud region and partition must match the IDP stack's.
**Decided (T047)**: package the SDK. Measured: `idp_sdk` imports cleanly with only boto3, pydantic, jsonschema, deepdiff, PyYAML, click, rich and anyio installed (about 75 MB with boto3, which the Lambda runtime already provides); the two first-party packages are copied from a local checkout of the IDP accelerator (`IDP_REPO` in `src/Makefile`) because `idp-common` on public PyPI belongs to someone else. No direct-read fallback is needed.

## R17. Failures
**Decision**: IDP errors map to refusals before any ERP work: `DOCUMENT_NOT_FOUND` (SDK `IDPResourceNotFoundError`), `DOCUMENT_NOT_READY` (state named), `DOCUMENT_INCOMPLETE` (missing fields named), `DOCUMENT_NEEDS_REVIEW` (low-confidence fields named), `DOCUMENT_CHANGED` (save only), `IDP_UNAVAILABLE` (any other SDK or network failure, FR-022). None reserves a number or writes a record.
