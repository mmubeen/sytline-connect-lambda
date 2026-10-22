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
