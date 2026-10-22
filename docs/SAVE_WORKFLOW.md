# Saving order data to SyteLine: how the workflow works

This describes exactly what happens when order information is saved to the ERP using reserved numbers (stage 2 of the job-number workflow). For the reserve step and the states, see [RESERVATIONS.md](RESERVATIONS.md). The code is `scr/syteline/savewf.py` (the steps), `scr/handlers/save_intake.py` (accepting the request) and `scr/handlers/worker.py` (running it).

> **Status of the field names.** The SyteLine property names and the update call below are best guesses until the read-only discovery task has run on the plant network (`scr/syteline/idos.py`, `SoapIdoClient.update`). Do not enable live writes before then.

## At a glance

```
caller                          API                       worker (in the plant network)            SyteLine
  |  POST /job-reservations      |                              |                                      |
  |----------------------------->| reserve CO + job numbers ----+------------------------------------->|  (numbers only)
  |<-- co_num, jobs[] -----------|                              |                                      |
  |  POST .../{id}/save          |                              |                                      |
  |----------------------------->| checks, record, 202          |                                      |
  |<-- 202 save_accepted --------|--- invoke (async) ---------->| 1. insert CO header                 |
  |                              |                              | 2. per line:                         |
  |                              |                              |    a. insert CO line (ref = job #)   |
  |                              |                              |    b. update teardown date           |
  |                              |                              |    c. insert job                     |
  |                              |                              | 3. read back and verify              |
  |  GET .../{id}  (poll)        |                              |                                      |
  |<-- state + per-line results -|                              |                                      |
```

Saving is asynchronous. The request returns `202` as soon as it is recorded; the write to SyteLine happens afterwards in a single worker that handles one save at a time, so ERP writes are never concurrent.

## 1. Before saving: the request is checked

`POST /job-reservations/{id}/save` is refused, with nothing written, when any of these is true (checked in this order):

| Problem | Status / code |
|---|---|
| Body invalid (missing `confirmed: true`, `uploaded_by`, `po_number`, customer name, lines; bad quantity, UOM, dates, prices; more than 25 lines) | `400 VALIDATION` (all problems listed together) |
| No such reservation | `404 NOT_FOUND` |
| Made by a different caller (same role with another session is fine) or a different `uploaded_by` | `403 RESERVATION_FORBIDDEN` |
| Still being reserved, or the reservation failed | `409 RESERVATION_NOT_READY` |
| Already saved, or a save is already queued, running or finished | `409 RESERVATION_USED` |
| Customer, PO number or number of lines differ from the reservation (case and spacing are ignored) | `422 RESERVATION_MISMATCH` |
| The save request could not be recorded | `503 STORE_UNAVAILABLE` (the reservation stays usable) |

Otherwise the reservation moves from `reserved` to `save_accepted` in one conditional step (this is what makes it single-use), the worker is started, and the caller gets `202`. The customer is **not** taken from the request: it is the customer matched when the numbers were reserved.

If the worker cannot be started, the reservation stays `save_accepted` and a scheduled sweeper starts it after about two minutes. Nothing can have been written at that point, so this is safe.

## 2. What the worker writes, in order

The worker claims the reservation (`save_accepted` to `saving`; a second claim is refused, so a save never runs twice). It then does the following. It **never obtains a new number**; the CO and job numbers come from the reservation.

| # | Step | What is written | If it fails |
|---|---|---|---|
| 0 | Live-writes check | nothing | `failed` (`LIVE_WRITES_DISABLED`), nothing written |
| 1 | **CO header** (`SLCos`) | CO number, customer number and sequence, order date (request value or today), status `O`, customer PO number (from `po_number`), received date if given | rejected and proven absent: `failed`; otherwise see "Outcomes" |
| 2a | **CO line** (`CCT_Coitems`), per line, in order | CO number, line number, item, UOM, quantity, due date and price if given, and the job reference: type `J`, **reference number = the line's reserved job number**, suffix 0, release 0 | see "Outcomes" |
| 2b | **Teardown date**, only if the line has `teardown` | a separate update of that CO line | a rejection is a **warning** on the line and the run goes on; any other failure stops the run as uncertain |
| 2c | **Job** (`CCTJobs`) | job number, order type and warehouse (request value, else the deployment default), item, quantity released, CO number and line (the link back), root job = the job number, engine serial, serial number and packing if given | see "Outcomes" |
| 3 | **Read-back** | nothing (reads only) | see below |

Each row is recorded as "possibly written" **before** it is sent. If the worker stops at any moment, the record shows exactly which rows might exist in SyteLine.

The link between a CO line and its job is held in both directions: the line carries the job number, and the job carries the CO number and line.

### Read-back and "saved"

After the last write, the worker reads back the CO header, the CO lines, and each job. A line is reported `saved` only when all of these hold:

- the CO header exists once, with the matched customer;
- the CO line exists with the requested item and quantity;
- the CO line references its reserved job number;
- the job exists once and points back to that CO and line.

Anything missing, duplicated or different leaves the line `unverified` with a read-back note, and the whole save ends `needs_reconciliation`. If the read cannot be completed at all, nothing is reported saved.

## 3. Outcomes

The save stops at the first problem (it does not carry on after an unknown result).

| Final state | When | Per-line results | What to do |
|---|---|---|---|
| `saved` | every line confirmed by read-back (teardown warnings may remain) | all `saved` | nothing |
| `partly_saved` | a **CO line insert was rejected** by SyteLine, its rows were proven absent by read-back, at least one earlier line is saved and confirmed | earlier `saved`, the rejected line `failed` (with the error), the rest `not_attempted` | decide by hand: add the remaining lines in SyteLine, or leave them |
| `failed` | live writes off, no time left, or the header was rejected and proven absent, before anything was written | all `not_attempted` | reserve again with a new `request_ref` (these numbers are consumed) |
| `needs_reconciliation` | a timeout or unknown result; a job rejected after its CO line went in (a line without a job exists); a first-line rejection (the header exists with no lines); read-back mismatch or failure; a worker that stopped | what is known per line, `unverified` where a row may exist | check SyteLine for the rows listed in `possibly_written`; nothing is retried or deleted automatically |

Rules that always hold:

- A save is never replayed. Only `save_accepted` reservations (nothing yet sent) are ever started again.
- Nothing is ever deleted from SyteLine. The client has no delete operation.
- Numbers reserved but not saved stay consumed; the status view marks them `consumed_unused` or `unverified` so the gap can be explained.

## 4. Calling it

Request (the reservation id is in the path; `customer`, `po_number` and the number of `lines` must match the reservation):

```json
POST /job-reservations/6f1c.../save
{"confirmed": true, "uploaded_by": "jdoe",
 "customer": {"name": "ACME AEROSPACE", "address1": "1 MAIN ST", "city": "PHOENIX", "state": "AZ", "postal_code": "85001"},
 "po_number": "PO-77812", "order_date": "2026-10-20", "received_date": "2026-10-19",
 "lines": [
   {"item": "ABC-100", "quantity": 5, "uom": "EA", "due_date": "2026-11-01", "price": 12.5,
    "teardown": "2026-10-25", "engine_sn": "E123", "serial_num": "S456", "packing": "BOX"},
   {"item": "XYZ-200", "quantity": 1, "uom": "EA"}]}
```

Immediate answer: `202 {"reservation_id": "6f1c...", "state": "save_accepted"}`.

Poll `GET /job-reservations/6f1c...` until the state is final (`saved`, `partly_saved`, `failed` or `needs_reconciliation`):

```json
{"reservation_id": "6f1c...", "state": "saved", "co_num": "100",
 "jobs": [{"line": 1, "job": "5000", "usage": "used"}, {"line": 2, "job": "5001", "usage": "used"}],
 "lines": [
   {"line": 1, "job": "5000", "co_line": 1, "item": "ABC-100", "status": "saved", "warnings": []},
   {"line": 2, "job": "5001", "co_line": 2, "item": "XYZ-200", "status": "saved", "warnings": []}]}
```

A warning looks like `"teardown date not set: <ERP text>"`. Every request and the worker's run are logged as one structured line (type, identifiers, outcome, duration); credentials never appear in logs or responses.

## 5. How this differs from the one-step upload

| | `POST /confirmed-orders` (feature 001) | Reserve then save (feature 004) |
|---|---|---|
| Numbers | obtained inside the upload, after the order is accepted | reserved first and returned to the caller, then used by the save |
| Teardown date | part of the CO line insert | separate update; a rejection is only a warning |
| Extra fields | none | PO number and received date on the header; engine serial, serial number, packing on the job |
| Link check | line and job rows exist | also that the line references its job and the job points back |
| Failure outcomes | `failed` or `needs_reconciliation` | adds `partly_saved` |

Both use the same ERP safety rules, the same single worker and the same jobs table; either can be used, but one PO should go through only one of them.

## 6. Where to look when something is wrong

| Symptom | Likely cause |
|---|---|
| `409 RESERVATION_USED` on a first attempt | a save was already accepted (a retry that crossed with the first request); poll the status instead |
| `422 RESERVATION_MISMATCH` | the PO details differ from what was reserved; reserve again for the changed PO |
| Stays `save_accepted` for more than a few minutes | the worker could not start; the sweeper retries every 5 minutes; check the worker's logs |
| `failed` with `LIVE_WRITES_DISABLED` | live writes are off for the deployment |
| `needs_reconciliation` with `STALE_RUNNING` | the worker stopped mid-run; check SyteLine for the rows in `possibly_written` |
| `needs_reconciliation` with `READBACK_MISMATCH` | rows exist but differ from the request, or could not be read back; the line notes say what differed |
| Warnings about the teardown date | SyteLine rejected the update; set the date by hand |
