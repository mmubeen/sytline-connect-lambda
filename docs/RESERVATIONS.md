# Job-number reservations (two-stage save)

An alternative to the one-step `POST /confirmed-orders` upload, available in the **VPC deployment only** (the stand-alone version has no job table). You first **reserve** the CO and job numbers from SyteLine, see them, and then **save** the order information using exactly those numbers. It follows the PO OCR demo's flow: the job number is never looked up, SyteLine's own numbering routine reserves the next free one, and that number links the CO line to the job.

Design and decisions: [`specs/004-job-number-workflow/`](../specs/004-job-number-workflow/) (spec, plan, research, data model, [API contract](../specs/004-job-number-workflow/contracts/api.md)). Same private API, same `AWS_IAM` authorization as the order upload.

## The flow

1. `POST /job-reservations` with `request_ref`, `confirmed: true`, `uploaded_by`, the `customer` details, `po_number` and `line_count` (1 to 25). SyteLine matches the customer (it must be exactly one confident match), then CO and job numbers are reserved: you get `co_num` and one `job` per line, in line order. **Nothing else is written** to SyteLine. Sending the same `request_ref` and request again returns the same numbers (no new ones).
2. Use the numbers (for example to name the filed PO copy after the job).
3. `POST /job-reservations/{id}/save` with the PO details (customer, `po_number`, `lines`, optional dates, warehouse, order type; per line optional due date, price, teardown date, engine serial, serial number, packing). It answers `202` and a background run saves: the CO header, then per line the CO line (carrying its job number), the line's teardown date, and the job; then it reads everything back.
4. `GET /job-reservations/{id}` shows the state, the numbers with what became of each, and the per-line results.

A reservation can be saved **once**, **for the PO it was made for** (same customer, PO number and number of lines) and **by the caller that made it** (same role, same `uploaded_by`). Anything else is refused before anything is written (`RESERVATION_USED`, `RESERVATION_MISMATCH`, `RESERVATION_FORBIDDEN`, `RESERVATION_NOT_READY`).

Reserving is itself a change in SyteLine, so it needs live writes switched on (`SYTELINE_LIVE_WRITES`), exactly like any other write.

## States

| State | Meaning | What to do |
|---|---|---|
| `reserving` | numbers are being obtained (a reserve that never finished reads as `failed`) | wait; retry with the same `request_ref` returns the current state |
| `reserved` | numbers held, nothing saved | save it, or leave it: numbers never expire |
| `save_accepted` / `saving` | the save is queued / running | poll |
| `saved` | every line, job and link confirmed by read-back | nothing |
| `partly_saved` | a line was rejected by SyteLine and its rows were proven absent; earlier lines are saved and confirmed, later lines were not attempted | a person decides: finish the remaining lines in SyteLine by hand, or leave them |
| `failed` | the reservation or the save failed before anything was written | reserve again with a new `request_ref`; the numbers of this one are gone |
| `needs_reconciliation` | the outcome is not proven (timeout, a rejection that left a row behind, a read-back mismatch, a worker that stopped) | check SyteLine for the records listed in `possibly_written`; **nothing is deleted or retried automatically** |

## Numbers that were consumed

SyteLine does not take a reserved number back. If a reservation is never saved, or its save fails, the numbers stay used and leave a gap, which matches how SyteLine behaves for its own users. The status view says what became of each number (`jobs[].usage`): `reserved` (waiting), `used` (line saved and confirmed), `consumed_unused` (gone, no record written), `unverified` (a record may exist: check SyteLine). A failed reserve response also lists `consumed_unused`.

## Stopping rule

The save stops at the first problem, as the order upload does; it never carries on after an unknown outcome. A rejected teardown date is only a warning on its line (the job is still saved). A timeout while setting it stops the run as `needs_reconciliation`.

## Limits and notes

- At most 25 lines per reservation (the reserve call must answer inside API Gateway's 29 s limit; it also stops safely, keeping the numbers it got, if time runs short).
- Not included: filing the scanned PO in a job folder on a network share (the caller can do it using the returned job numbers), and the demo's default terms and ship-via from the customer record.
- **Unconfirmed names:** the property names written by the save (customer PO number, received date, the line's job reference, teardown, root job, engine serial, serial number, packing) and the update call are guesses until the read-only discovery task is run on the plant network. They are all in `scr/syteline/idos.py` and `SoapIdoClient.update` in `scr/syteline/client.py`. Do not enable live writes for this before then.
