# Job-number reservations (two-stage save)

An alternative to the one-step `POST /confirmed-orders` upload, available in the **VPC deployment only** (the stand-alone version has no job table). You first **reserve** the CO and job numbers from SyteLine, see them, and then **save** the order information using exactly those numbers. It follows the PO OCR demo's flow: the job number is never looked up, SyteLine's own numbering routine reserves the next free one, and that number links the CO line to the job.

How the save to SyteLine works step by step: [SAVE_WORKFLOW.md](SAVE_WORKFLOW.md).

Design and decisions: [`specs/004-job-number-workflow/`](../specs/004-job-number-workflow/) (spec, plan, research, data model, [API contract](../specs/004-job-number-workflow/contracts/api.md)). Same private API, same `AWS_IAM` authorization as the order upload.

## The flow

The PO details (customer, PO number and lines) are **never sent by the caller and never stored**. They are read from the **IDP document** that holds the extraction result, through the IDP SDK, by `document_id` (the S3 key `batch-id/filename`).

1. `POST /job-reservations` with `request_ref`, `confirmed: true`, `uploaded_by` and `document_id`. The document is read first and must be usable (below). SyteLine then matches the customer from the document (it must be exactly one confident match: by the web service, or, when no endpoint is configured, the database search with the PO OCR demo's scoring), and CO and job numbers are reserved (by the web service or by SyteLine's own numbering routines in the database; see [DATABASE.md](DATABASE.md)): you get `co_num` and one `job` per line, in line order. **Nothing else is written** to SyteLine. Sending the same `request_ref` and request again returns the same numbers (no new ones).
2. Use the numbers (for example to name the filed PO copy after the job).
3. `POST /job-reservations/{id}/save` with `confirmed`, `uploaded_by`, the same `document_id`, and optionally `order_date`, `received_date`, `warehouse`, `order_type`. It answers `202` and a background run reads the document again, then saves: the CO header (with the customer's terms and ship-via from its most recent order, when found), then per line the CO line (carrying its job number), the line's teardown date, and the job; then it reads everything back.
4. `GET /job-reservations/{id}` shows the state, the numbers with what became of each, the per-line results and any order-level warnings (for example that the customer's defaults were not available).

### When a document can be used

Before anything is reserved or written, the document must (1) have finished processing (a document still `HITL_IN_PROGRESS` or `RUNNING` is refused, naming the state), (2) contain every field the save needs, and (3) have every such field the IDP assessed at or above its confidence threshold. Fields below their threshold are refused (`DOCUMENT_NEEDS_REVIEW`, listing them): review them in IDP first, then try again. Refusals: `DOCUMENT_NOT_FOUND`, `DOCUMENT_NOT_READY`, `DOCUMENT_INCOMPLETE`, `DOCUMENT_NEEDS_REVIEW`, `TOO_MANY_LINES`, `IDP_UNAVAILABLE`; none reserves a number or writes a record.

The document is read at reserve, at save intake and when the worker runs. If its customer, PO number or number of lines changed since the reservation, the save is refused (`DOCUMENT_CHANGED`). A correction that keeps those three (for example a quantity fixed in review) is used as it stands when the worker writes.

A reservation can be saved **once**, **for the document it was made for** and **by the caller that made it** (same role, same `uploaded_by`). Anything else is refused before anything is written (`RESERVATION_USED`, `RESERVATION_MISMATCH`, `RESERVATION_FORBIDDEN`, `RESERVATION_NOT_READY`).

Reserving is itself a change in SyteLine, so it needs live writes switched on (`SYTELINE_LIVE_WRITES`), exactly like any other write.

### Setting up the IDP source

- Stack parameters `IdpStackName`, `IdpOutputBucketName`, `IdpTrackingTableName` (all three or none) and optionally `IdpRegion`. Without them reserve and save answer `IDP_UNAVAILABLE`. The reserve, save-intake and worker functions get read-only access to that stack's description, its output bucket and its tracking table (same account; cross-account access is not set up).
- The functions run in the plant-routed network, so they need a private path to **S3 and CloudFormation** (VPC endpoints or a NAT route). This stack creates neither.
- The IDP SDK is packaged with the functions at build time from a checkout of the IDP accelerator: `IDP_REPO=/path/to/accelerated-intelligent-document-processing-on-aws sam build`. Without `IDP_REPO` the build warns and the SDK is missing. The names `idp-common` and `idp-sdk` must never be installed from public PyPI (the first belongs to someone else); the build copies the code instead.
- **Field names are unconfirmed.** `src/syteline/idp_fields.py` holds the defaults (`customer_name`, `cust_po`, `lines[].part_number` ...), taken from the PO OCR demo. Set `IDP_DOCUMENT_CLASS` and `IDP_FIELD_MAP` (a JSON object, for example `{"po_number": "purchase_order"}`) or edit that file once the real class and field names are known.

### Where the numbers and the customer come from

For each operation the web service is used when an endpoint is configured; otherwise the SyteLine database is (the PO OCR demo's calls). See [DATABASE.md](DATABASE.md). The customer search refuses ambiguous, low-confidence and unknown customers (`CUSTOMER_AMBIGUOUS`, `CUSTOMER_LOW_CONFIDENCE`, `CUSTOMER_NOT_FOUND`) with the best candidates and their scores; an unknown outcome while reserving a number is `UNCERTAIN_OUTCOME` and is never retried.

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

- At most 25 lines per reservation (the reserve call must answer inside API Gateway's 29 s limit; it also stops safely, keeping the numbers it got, if time runs short; the IDP document read adds a few seconds).
- Not included: filing the scanned PO in a job folder on a network share (the caller can do it using the returned job numbers). The demo's default terms and ship-via are included: the save reads them from the customer's most recent order (see [DATABASE.md](DATABASE.md)); when they can't be read the order is saved without them and the status view shows a warning.
- **Unconfirmed IDP names and review rule:** the IDP field names above, and how a human-reviewed result is recognised (the SDK has no review operation; a reviewed document is assumed to be `COMPLETED` and to report its corrected values meeting their thresholds). Check this on your IDP stack before relying on it.
- **Unconfirmed ERP names:** the property names written by the save (customer PO number, received date, terms code, ship-via code, the line's job reference, teardown, root job, engine serial, serial number, packing), the update call and the database names listed in [DATABASE.md](DATABASE.md) are guesses until the read-only discovery task is run on the plant network. They are all in `src/syteline/idos.py` and `SoapIdoClient.update` in `src/syteline/client.py`. Do not enable live writes for this before then.
