# Research: SyteLine ERP Integration

Source of truth for ERP behavior is `docs/Instruction Sheet AWS GovCloud API Gateway to On-Premises Sy.docx` (a **proposed** design; its own go/no-go requires Chromalloy IT sign-off on CIDRs, VPN routing, DNS, SQL listener, HTTP over the private path, secrets and reconciliation). It cites the PO OCR Program Technical Handoff, which this repo does not contain.

## R1 — Overall shape
- **Decision**: Private REST API → intake Lambda → job record → worker Lambda (VPC) → SyteLine (SOAP + SQL). Status route reads the job record.
- **Rationale**: Matches sheet §§3–4 and the clarified async answer. API Gateway must not talk to SOAP directly; the SQL resolver, numbering, multi-IDO writes and read-back are required.
- **Alternatives**: Synchronous Lambda behind the API (timeouts land in the middle of writes — rejected); API Gateway→SOAP mapping (rejected by the sheet).

## R2 — Network and API
- **Decision**: REST API of type **Private**, `execute-api` interface endpoint, resource policy restricting `aws:SourceVpce`, authorization `AWS_IAM`; worker Lambda in private subnets with routes to the plant over the VPN; SOAP over HTTP port 80 as documented; SQL port a required configuration value with no default.
- **Rationale**: Sheet §§2–3. Endpoint policy is not a substitute for authentication (FR-017).
- **Open (Chromalloy IT)**: CIDRs, VPN routing, plant DNS resolution from the worker, SQL host/port, secret store.

## R3 — Async execution
- **Decision**: Intake writes the job (`accepted`) and the `request_ref` index atomically, then invokes the worker asynchronously with retries disabled. A scheduled sweeper re-invokes jobs still `accepted` (nothing can have been written) and moves `running` jobs older than the worker timeout to `needs_reconciliation`. Worker reserved concurrency 1.
- **Rationale**: Durable acceptance (FR-015) without redelivery semantics that could replay an upload. Serial writes reduce contention on numbering.
- **Alternatives**: SQS (visibility-timeout redelivery is an automatic replay unless carefully neutralized); Step Functions (heavier than needed).

## R4 — SQL access
- **Decision**: `python-tds` (pure Python) in the Lambda package.
- **Rationale**: No native ODBC driver to package for Lambda. The sheet says the existing adapter uses ODBC/SQL; any SQL Server client satisfies it.
- **Alternatives**: `pyodbc` + Microsoft ODBC driver layer (heavier, driver licensing); `pymssql`. Revisit if SQL Server's TLS/auth settings are incompatible (verify read-only in the network check).

## R5 — Pipeline (sheet §4)
1. Resolve customer from `custaddr` (SQL, read-only) → exactly one match, else reject (nothing written, no number allocated).
2. **If live writes are disabled: stop here** with `LIVE_WRITES_DISABLED` (allocating numbers also writes to the ERP).
3. `NextCoSp` (SQL) → insert `SLCos` (SOAP).
4. Per line: `NextWOSp` (SQL) → insert `CCT_Coitems` line with teardown value in `UserDefinedFields` → insert `CCTJobs` job.
5. Read back `SLCos` (and the lines/jobs) and compare with what was sent.
6. Mark `succeeded` only if every line and job verified.
- **Diffgram rule**: HTTP 200 is not acceptance. Insert responses are parsed for row-level errors.

## R6 — Uncertainty classification (clarified FR-004)
- **Definite failure (retry allowed)**: failure before any SOAP insert was sent (validation, no/ambiguous customer match, login failure, live writes disabled), **or** an insert error followed by a successful read-back proving no rows exist for the CO number.
- **Uncertain → `needs_reconciliation`**: any error/timeout/ambiguous response after an insert was sent where read-back cannot prove absence, any partial presence, or read-back failure. The job lists the CO number, line numbers and job numbers that may exist. No ERP delete, no automatic retry; a person resolves it and explicitly closes or re-submits it.
- **Rationale**: Sheet §5 — the path is insert-only and timeouts can follow accepted inserts.

## R7 — Idempotency
- **Decision**: `request_ref` → `job_id` index with a conditional put. Same `request_ref` and same payload hash → return the existing job (`duplicate:true`). Same `request_ref` with a different payload → 409. A job in `needs_reconciliation` is never replayed.

## R8 — Live-write gate (clarified FR-016)
- **Decision**: `SYTELINE_LIVE_WRITES` (default `false`). When false the worker performs only read-only steps. The dry-run health check never opens SQL. Discovery of SOAP operation/property names must use read-only calls (e.g. WSDL inspection, `LoadDataSet`) until the ERP owner approves a live test with a reconciliation and cleanup plan.

## R9 — Auth and attribution (clarified FR-017)
- **Decision**: `AWS_IAM` on both routes. Request carries `uploaded_by` (required); intake also records `requestContext.identity.userArn`. Only `confirmed: true` orders are accepted.

## R10 — Notes
- **Decision**: Keep `add_note`/`get_notes` as direct-invoke actions, implemented over SQL using `CreateSpecificNoteSp` (creates and attaches in one call; internal flag stored as `NoteFlag`) and the queries in `sql/NotesExampleQueries.txt`. `add_note` is a write and is gated by `SYTELINE_LIVE_WRITES`.
- **Open**: the instruction sheet does not mention notes; confirm transport with the ERP owner.

## R11 — Health check
- **Decision**: Unchanged stages; add an optional `sql` TCP-connect stage when SQL settings are present, never in dry run.

## Open items (do not block design; block live use)
1. Customer matching rules: the instruction sheet defers to the existing PO OCR resolver. Plan assumes exact normalized name + address against `custaddr`; align with the resolver's owner.
2. Source and format of the per-line teardown value written to `UserDefinedFields` (treated as an optional pass-through string per line until confirmed).
3. Exact SOAP operation names and payload shapes, and the `CCT_Coitems`/`CCTJobs` required properties — discover read-only (workbook lists `CCTJobs` required: Job, OrdType, Whse; `CCT_Coitems` is not in the workbook).
4. SQL host, listener port, TLS/auth mode, and SQL account permissions (EXECUTE on `NextCoSp`/`NextWoSp`, SELECT on `custaddr`).
5. VPN/DNS/CIDR sign-offs listed in the instruction sheet.
