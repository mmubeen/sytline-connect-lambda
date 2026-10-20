# Runbook: upload jobs in `needs_reconciliation`

An upload ends in `needs_reconciliation` when SyteLine may already hold some of its rows and the
service could not prove otherwise (timeout after a request was sent, rejection after earlier rows were
written, read-back failure or mismatch, or a worker that stopped mid-upload). The service **never
deletes ERP rows and never retries these automatically.**

## 1. Read the job
`GET /upload-jobs/{id}` (the `id` is the upload ID) returns `failure` (`code`, `message`, `step`) and `possibly_written`, a list of
`{kind, key}` entries: `co` (CO number), `line` (`<CO>/<line>`), `job` (job number).

| code | meaning |
|---|---|
| `UNCERTAIN_OUTCOME` | a request timed out or its result is unknown |
| `ERP_REJECTED` | the ERP refused a row after earlier rows were written |
| `READBACK_MISMATCH` | the verification read-back differs from what was sent (message lists what) |
| `STALE_RUNNING` | the worker stopped before finishing (timeout, crash) |
| `TIME_BUDGET_EXHAUSTED` | stopped on purpose because the Lambda was about to time out |

## 2. Check SyteLine (read-only)
For each entry in `possibly_written`, look it up in SyteLine by number: the CO (`SLCos`), its lines
(`CCT_Coitems`) and the jobs (`CCTJobs`). Record which exist and whether quantities/items match the
request (`request` is retained on the job).

## 3. Decide
- **Everything present and correct** → the upload effectively succeeded. Record the CO and job numbers
  against the order in the application, and tell the ERP owner the job can be closed as succeeded.
- **Nothing present** → nothing was written. The order may be submitted again.
- **Partly present** → the ERP owner decides whether to complete or remove the partial records **in
  SyteLine**. Do not resubmit until they are reconciled, or a duplicate order will result.

## 4. Closing the job
Closing a job (marking it succeeded/failed) and authorizing a resubmission with the same `request_ref`
are **not yet implemented** (analysis finding I2). Until they are, a person must coordinate with the
service owner, and a corrected resubmission must use a new `request_ref`.

## Rules
- Never submit the same order again while any of its records may exist.
- Live writes stay disabled (`SYTELINE_LIVE_WRITES=false`) except in a window approved by the ERP owner.
