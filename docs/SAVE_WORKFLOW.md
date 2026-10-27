# Saving order data to SyteLine: how the workflow works

This describes exactly what happens when order information is saved to the ERP using reserved numbers (stage 2 of the job-number workflow). For the reserve step and the states, see [RESERVATIONS.md](RESERVATIONS.md). The code is `src/syteline/savewf.py` (the steps), `src/handlers/save_intake.py` (accepting the request), `src/handlers/worker.py` (running it), `src/syteline/idp.py` (reading the PO details from the IDP document) and `src/syteline/defaults.py` (the customer's terms and ship-via). Where the numbers and the customer come from (the web service, or the SyteLine database when no endpoint is configured) is in [DATABASE.md](DATABASE.md).

> **Status of the names.** The SyteLine property names and the update call below (including the header's `TermsCode` and `ShipCode`), and the database types, routines and columns that [DATABASE.md](DATABASE.md) lists, are best guesses until they have been verified read-only on the plant network (`src/syteline/idos.py`, `SoapIdoClient.update`). Do not enable live writes before then.

## At a glance

```
caller                          API                       worker (in the plant network)            SyteLine
  |  POST /job-reservations      |                              |                                      |
  |----------------------------->| read IDP document, match the |                                      |
  |                              | customer, reserve CO + job --+------------------------------------->|  (numbers only; web service
  |                              | numbers (web service or db)  |                                      |   or database, see DATABASE.md)
  |<-- co_num, jobs[] -----------|                              |                                      |
  |  POST .../{id}/save          |                              |                                      |
  |----------------------------->| checks, record, 202          |                                      |
  |<-- 202 save_accepted --------|--- invoke (async) ---------->| 0. read the customer's defaults      |
  |                              |                              | 1. insert CO header (+ terms/ship)   |
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
| Body invalid (missing `confirmed: true`, `uploaded_by`, `document_id`; bad dates; `customer`, `po_number` or `lines` sent, which are refused because they come from the document) | `400 VALIDATION` (all problems listed together) |
| No such reservation | `404 NOT_FOUND` |
| Made by a different caller (same role with another session is fine) or a different `uploaded_by` | `403 RESERVATION_FORBIDDEN` |
| Still being reserved, or the reservation failed | `409 RESERVATION_NOT_READY` |
| Already saved, or a save is already queued, running or finished | `409 RESERVATION_USED` |
| `document_id` differs from the reserved document | `422 RESERVATION_MISMATCH` |
| The IDP document cannot be used now: not found, not finished, a needed field missing, needed fields below their confidence threshold, more than 25 lines, or IDP unreachable | `404/409/422/502` with `DOCUMENT_NOT_FOUND`, `DOCUMENT_NOT_READY`, `DOCUMENT_INCOMPLETE`, `DOCUMENT_NEEDS_REVIEW`, `TOO_MANY_LINES`, `IDP_UNAVAILABLE` |
| The document's customer, PO number or number of lines differ from the reservation (case and spacing are ignored) | `422 DOCUMENT_CHANGED` |
| The save request could not be recorded | `503 STORE_UNAVAILABLE` (the reservation stays usable) |

Otherwise the reservation moves from `reserved` to `save_accepted` in one conditional step (this is what makes it single-use), the worker is started, and the caller gets `202`. The customer is **not** taken from the request or the document at write time: it is the customer matched when the numbers were reserved. Nothing from the document is stored; the request records only the document ID and the optional order date, received date, warehouse and order type.

If the worker cannot be started, the reservation stays `save_accepted` and a scheduled sweeper starts it after about two minutes. Nothing can have been written at that point, so this is safe.

## 2. What the worker writes, in order

The worker claims the reservation (`save_accepted` to `saving`; a second claim is refused, so a save never runs twice). It then **reads the PO details from the IDP document again** (so it writes what the document says now, for example a quantity corrected in review) and checks that its customer, PO number and line count still match the reservation: if the document is no longer usable or no longer matches, the reservation ends `failed` with nothing written (an IDP source that is merely unavailable puts the save back to `save_accepted`; the sweeper retries it, up to 5 attempts). It then does the following. It **never obtains a new number**; the CO and job numbers come from the reservation.

| # | Step | What is written | If it fails |
|---|---|---|---|
| 0 | Live-writes check | nothing | `failed` (`LIVE_WRITES_DISABLED`), nothing written |
| 0b | **Customer defaults** | nothing (a read only): the terms code and ship-via code of the customer's most recent order, from the same ship-to first, otherwise any order; through the database or the web service as configured (`DefaultsIdo`); not read at all when that setting is absent | never blocks the save: no previous order or an unreadable default leaves the header without them and adds an order-level **warning** (shown by the status view) |
| 1 | **CO header** (`SLCos`) | CO number, customer number and sequence, customer PO number, **order date = the date the PO was received** (else the order date, else today), terms code and ship-via code from step 0b when found, and the fixed defaults `Stat` P, `Whse` MAIN, `TaxCode1` R, `FrtTaxCode1` N, `MscTaxCode1` N, `OrigSite` CCT. Key values are right-justified to the ERP's widths (CoNum 10, CustNum 7, CustSeq 4, CustPo 22). | a definite rejection with nothing written: `failed`; otherwise `needs_reconciliation` |
| 2a | **CO line** (`CCT_Coitems`, lowercase column names), per line, in order | `co_num`, `co_line`, `co_release` 0, `item` (30 wide), `qty_ordered` and `qty_ordered_conv`, `u_m`, `whse`, `stat` P, `ship_site` CCT, `due_date` if given, and the job reference `ref_type` J, **`ref_num` = the line's reserved job number**, `ref_line_suf` 0, `ref_release` 0. Price is not written. | a definite rejection: the line is `failed` (`partly_saved` when earlier lines are proven saved); otherwise `needs_reconciliation` |
| 2b | **Teardown date**, only if the line has `teardown` | a **`UserDefinedFields` row** for the line (`RowId` = the line's `RowPointer` read from `SLCoitems`, `TableName` `coitem`, `UDFDT1` = the date) | a rejection (or no RowPointer found) is a **warning** on the line and the run goes on; any other failure stops the run as uncertain |
| 2c | **Job** (`CCTJobs`) | job number, `Suffix` 0, `Type` J, `Stat` F, item, warehouse (request value, else the deployment default), quantity released, `OrdType` **O**, `OrdNum` / `OrdLine` / `OrdRelease` (the link back to the CO line), `RootJob` = the job number, `RootSuf` 0, `JobDate` (the received date), `JschStartDate` and `JschEndDate` (the line's due date, else the received date), and `uf_eng_sn`, `uf_sn`, `uf_app` (packing: Box -> CDBD, Crate -> YES) if given | see "Out
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

Request (the reservation id is in the path; `document_id` must be the one the numbers were reserved for; the other fields are optional and not PO data):

```json
POST /job-reservations/6f1c.../save
{"confirmed": true, "uploaded_by": "jdoe", "document_id": "batch-20261021/PO-77812.pdf",
 "order_date": "2026-10-20", "received_date": "2026-10-19", "warehouse": "MAIN", "order_type": "J"}
```

Immediate answer: `202 {"reservation_id": "6f1c...", "state": "save_accepted"}`.

Poll `GET /job-reservations/6f1c...` until the state is final (`saved`, `partly_saved`, `failed` or `needs_reconciliation`):

```json
{"reservation_id": "6f1c...", "document_id": "batch-20261021/PO-77812.pdf", "state": "saved", "co_num": "100",
 "jobs": [{"line": 1, "job": "5000", "usage": "used"}, {"line": 2, "job": "5001", "usage": "used"}],
 "lines": [
   {"line": 1, "job": "5000", "co_line": 1, "item": "ABC-100", "status": "saved", "warnings": []},
   {"line": 2, "job": "5001", "co_line": 2, "item": "XYZ-200", "status": "saved", "warnings": []}]}
```

Warnings come in two kinds. A line warning looks like `"teardown date not set: <ERP text>"` (inside `lines[].warnings`). An order-level warning looks like `"customer defaults not available: no previous order for this customer"` and appears in a top-level `warnings` list on the status view (absent when empty). Every request and the worker's run are logged as one structured line (type, identifiers, outcome, duration), and each database-backed step logs its own line (type, outcome, duration, candidate count); credentials never appear in logs or responses.

## 5. How this differs from the one-step upload

| | `POST /confirmed-orders` (feature 001) | Reserve then save (feature 004) |
|---|---|---|
| Numbers | obtained inside the upload, after the order is accepted | reserved first and returned to the caller, then used by the save |
| Teardown date | part of the CO line insert | separate update; a rejection is only a warning |
| PO details | sent by the caller and stored with the order | read from the IDP document; never sent, never stored |
| Extra fields | none | PO number and received date on the header; engine serial, serial number, packing on the job |
| Link check | line and job rows exist | also that the line references its job and the job points back |
| Customer | matched in SyteLine (web service) | matched through the web service or, when no endpoint is configured, the database search with the demo's scoring; only one confident match is accepted |
| Header terms and ship-via | none | the customer's most recent order's codes, when found (warning when not) |
| Numbers | the web service or the database, per setting; an unknown outcome is `UNCERTAIN_OUTCOME` | the same, reserved before the save |
| Failure outcomes | `failed` or `needs_reconciliation` | adds `partly_saved` |

Both use the same ERP safety rules, the same single worker and the same jobs table; either can be used, but one PO should go through only one of them.

## 6. Where to look when something is wrong

| Symptom | Likely cause |
|---|---|
| `409 RESERVATION_USED` on a first attempt | a save was already accepted (a retry that crossed with the first request); poll the status instead |
| `422 DOCUMENT_CHANGED` | the document's customer, PO number or line count changed after reserving; reserve again for the changed document |
| `422 DOCUMENT_NEEDS_REVIEW` / `409 DOCUMENT_NOT_READY` | fields are below their confidence threshold or the document is still in review; finish the review in IDP and try again |
| `failed` with `IDP_UNAVAILABLE` after several attempts | the IDP source could not be read; check its network path and permissions, then reserve again |
| Stays `save_accepted` for more than a few minutes | the worker could not start; the sweeper retries every 5 minutes; check the worker's logs |
| `failed` with `LIVE_WRITES_DISABLED` | live writes are off for the deployment |
| `needs_reconciliation` with `STALE_RUNNING` | the worker stopped mid-run; check SyteLine for the rows in `possibly_written` |
| `needs_reconciliation` with `READBACK_MISMATCH` | rows exist but differ from the request, or could not be read back; the line notes say what differed |
| Warnings about the teardown date | SyteLine rejected the update; set the date by hand |
| Order-level warning "customer defaults not available" | the customer has no earlier order, or the defaults could not be read (database or web service); the order was saved without terms and ship-via, set them in SyteLine; check [DATABASE.md](DATABASE.md) if every order shows it |
| Reserve answers `DATABASE_NOT_CONFIGURED` (503) | an operation uses the database route and a `SQL_*` key is missing from the secret (`SQL_CA_PEM` counts only while the connection is encrypted, the default); the message names the keys; nothing was reserved |
| Reserve answers `CUSTOMER_AMBIGUOUS` / `CUSTOMER_LOW_CONFIDENCE` / `CUSTOMER_NOT_FOUND` (422) | the document's customer and address did not identify one confident SyteLine customer; the response lists the best candidates with scores (not found: none); correct the document in IDP and try again; nothing was reserved |
| Reserve answers `UNCERTAIN_OUTCOME` (502) | the database connection was lost while a number was being reserved; that number may be consumed (the response lists the numbers already obtained as `consumed_unused`); it is never retried: reserve again with a new `request_ref` |
| `ERP_UNREACHABLE` on a database step | cannot connect or log in: run `{"action":"health"}` and read `db_dns`, `db_tcp`, `db_login` (certificate, host name, login) |

## Where the field names and values come from

They follow the PO OCR demo (`Intern-Demo/.../COMPLETE_OCR`, `sytline/syteline/forms/*.py` and `formatting.py`), which is known to work against this ERP, and `docs/COJobCreationIDOs.xlsx`. The rows are built in one place, `src/syteline/rowbuild.py`.

- **Lines are written to `CCT_Coitems`** with lowercase column-style names (`co_num`, `co_line`, `u_m`, `ref_num` ...); the standard `SLCoitems` (PascalCase) is only **read back**. Writing PascalCase names to `CCT_Coitems` is why every name was refused there.
- **Key values are padded** (right-justified) to the column widths, and filters on them use the padded value too: CoNum / Job / OrdNum / ref_num 10, CoLine 4, CustNum 7, CustSeq 4, CustPo 22, Item 30.
- **SLCos has no received-date property**: the demo writes the received date as `OrderDate`.
- **Dates** are ISO datetimes at midnight with the Eastern offset, for example `2026-10-19T00:00:00-04:00`.
- **A job always links to its order**: `OrdType` is `O`; the configured order type is no longer written (`SYTELINE_DEFAULT_ORDTYPE` is not required any more).
- **Not done yet** (the demo does them): the unit of measure and due period from the customer contract, a default teardown date of the received date plus 7 days. A price is not written by the demo or here.

## Finding the right property names (read-only)

When the service refuses a property ("error property [X] does not exist in [IDO] IDO"), ask it which names an IDO accepts instead of guessing, one failed save at a time:

```
echo '{"action": "probe_properties", "ido": "CCT_Coitems"}' > /tmp/probe.json && ./invoke.sh /tmp/probe.json
```

To list the names an IDO declares instead of guessing (names only, from the schema of a one-row read; the payload goes in a file, `./invoke.sh FILE`):

```
{"action": "probe_properties", "ido": "CCT_Coitems", "list": true}
```

`probe_item` is a read-only look at how the ERP holds one item: `{"action": "probe_item", "item": "3810727-6", "co_num": "129556"}` (`co_num` is optional). It reads the item master (`SLItemcusts`) for `R* <item>` exactly, padded to 30 characters and with a `LIKE` on the part number, and with `co_num` the `Item` and `Description` of that order's lines. Each read returns up to 3 rows or the ERP's own refusal; nothing is written. Use it when a line saves with a blank item or description.

`probe_notes` is a read-only check that the notes were attached: `{"action": "probe_notes", "co_num": "129566"}`. It reads the order's `RowPointer`, its link rows (`SLObjectNotes`) and each linked note (`SpecificNotes`), and returns per note the token, header token, note type, title, creation date and the **length** of the text (never the text). `missing_titles` lists which of the five section titles the order has no note for, and `unlinked_recent` lists notes with a section title written today or yesterday that no link row ties to this order (for example one left by a failed run). Nothing is written. Run it after a dev save, and again after the repeat run to confirm nothing was duplicated.

Each name below is requested alone in a one-row read; the answer lists `exists` and `missing` (with the service's message) and never a value. `CCT_Coitems`, `CCTJobs` and `SLCos` have a default list of the names the save uses plus likely alternatives. Nothing is written, so live writes need not be on.

## The property pre-check

Before a reservation takes any number (and before a save writes the order header), the function asks the ERP, in single-property one-row reads, whether it has every property the save will write on `SLCos`, `CCT_Coitems` and `CCTJobs`. If any is refused, the reservation fails with `ERP_PROPERTIES_UNKNOWN` (step `check_properties`) and a message listing **every** missing name per IDO; nothing is reserved or written, so no number is burned and no order header without lines is left behind. Names that passed are remembered for the life of the container. The teardown date is not checked (a refused one is only a warning). Fix the names in `src/syteline/idos.py` (use `probe_properties` to find them) and send the document again. `SYTELINE_CHECK_PROPERTIES=false` on the function switches the check off.

## Development mode (`DEV_TARGET_CO`)

With `DEV_TARGET_CO` set the save does not insert the order: step `check_target` confirms the pre-created order exists and belongs to the document's customer; then `update_co` overwrites the header, each line is updated (`update_line_N`) or inserted when missing (`create_line_N`), and the job is updated (`update_job_N`) when `JOB_NUMBER` is set and the job exists, else created. A rejected first update means nothing changed (`failed`); a later one is `needs_reconciliation`. See [EVENT_INTAKE.md](EVENT_INTAKE.md#dev_target_co-in-deployenv-development-mode).

## The `R* ` item prefix

The save writes each line's item as the repair item code, as the demo does: `PW1234` becomes `R* PW1234`; an item that already starts with `R*` (any case) is left as it is, so it is never prefixed twice. The same code goes into the order line and the job, is what the read-back compares, and is what the status shows as the line's `item`. It is always on (no setting). The one-step upload does not add it. A code longer than 30 characters with its prefix is refused with `ITEM_TOO_LONG` (step `check_items`, naming the line) before a number is reserved and again before the first write; it is never truncated.

## The line description

Each order line and job carries a description read from the item master for the `R* ` item code, as the demo does: `SLItemcusts` is read for `Item,ItmDescription` (`Item = '<code>'`, one row; the demo uses it because `SLItems` may not be granted to the service account) and the text is written as `description` on the `CCT_Coitems` row and `Description` on the `CCTJobs` row. It is never taken from the document. When there is no description (the item is not found, it has none, or the read fails), the line is **still saved** with a blank description and that line's result carries the warning `no description found in the item master for "R* ..." (the line was saved with a blank description)`; the order outcome and the line's status are not affected, and the read is never retried. The description is not part of the read-back comparison. The IDO and property names are the demo's and have not been confirmed against this ERP: a wrong name shows up as that warning on every line. The one-step upload does not write a description.

## The order notes

When the document carries any of these text sections, the save attaches each non-blank one to the **customer order** as its own note, after the order, lines and jobs are saved and read back: `customer_requests` ("Customer Requirements"), `regulatory_compliance` ("Regulatory Compliance"), `shipping_information` ("Shipping Information"), `billing_information` ("Billing Information"), `work_to_be_performed` ("Work To Be Performed"). The field names can be changed with `IDP_FIELD_MAP` (keys `note.customer_requests` ...). The combined `erp_notes_summary` is not attached, and no note goes on a line or a job.

Each note follows the ERP owner's path: the order's `RowPointer` is read from `SLCos`; a `SpecificNotes` row is inserted (`NoteDesc` = the title, `NoteContent` = the full text) and its `SpecificNoteToken` taken from the answer (or found by title and text); then a link row is inserted into the link IDO (`SLObjectNotes`, or `SYTELINE_OBJECT_NOTE_IDO`) with `SpecificNoteToken`, `NoteHeaderToken` 31 (order; 115 job and 37 line are not used), `RefRowPointer` and `NoteType` 0. The IDO and property names are the owner's and are UNCONFIRMED here: check them with `probe_properties` and `"list": true`.

A note never changes the outcome. A refused note is a warning (`note "<title>" was not attached: <reason>`) and the next note is still tried; an unknown outcome or an unreachable ERP stops the remaining notes, and nothing is retried. The outcome has a `notes` object: `attempted`, `attached`, `not_attached` (`title`, `reason`, `uncertain`), `skipped_existing`, `replaced`. Note text is never in the outcome or in the logs. Text is sent whole (never cut); the ERP's own limit is not known.

A title the order already has is skipped. In development mode (`DEV_TARGET_CO`) its text is updated instead, so repeated runs leave one copy of each note; no note is ever deleted. The one-step upload writes no notes.
