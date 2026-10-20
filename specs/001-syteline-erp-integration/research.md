# Research: SyteLine ERP Integration

Source of truth for ERP behavior is `docs/Instruction Sheet AWS GovCloud API Gateway to On-Premises Sy.docx` (a **proposed** design; its own go/no-go requires Chromalloy IT sign-off on CIDRs, VPN routing, DNS, handling of HTTP across the private path, secrets and reconciliation). It cites the PO OCR Program Technical Handoff, which this repo does not contain. **Where the sheet uses SQL (customer lookup, number allocation), the clarified requirement FR-018 supersedes it: all database interaction goes through the SyteLine web service.**

## R1 — Overall shape
- **Decision**: Private REST API → intake Lambda → job record → worker Lambda (VPC) → SyteLine web service. Status route reads the job record.
- **Rationale**: Matches sheet §§3–4 and the clarified async answer. API Gateway must not talk to SOAP directly: the multi-step pipeline, number allocation, multi-IDO writes and read-back are required.
- **Alternatives**: Synchronous Lambda behind the API (timeouts land in the middle of writes — rejected); API Gateway→SOAP mapping (rejected by the sheet).

## R2 — Network and API
- **Decision**: REST API of type **Private**, `execute-api` interface endpoint, resource policy restricting `aws:SourceVpce`, authorization `AWS_IAM`; worker and direct-invoke Lambdas in private subnets with a route to the plant over the VPN; the only plant-bound traffic is HTTP port 80 to the web service.
- **Rationale**: Sheet §§2–3. Endpoint policy is not a substitute for authentication (FR-017). Dropping SQL removes the SQL firewall rule, port and host sign-offs.
- **Open (Chromalloy IT)**: CIDRs, VPN routing, plant DNS resolution from the worker, secret store, HTTP over the private path.

## R3 — Async execution
- **Decision**: Intake writes the job (`accepted`) and the `request_ref` index atomically, then invokes the worker asynchronously with retries disabled. A scheduled sweeper re-invokes jobs still `accepted` (nothing can have been written) and moves `running` jobs older than the worker timeout to `needs_reconciliation`. Worker reserved concurrency 1.
- **Rationale**: Durable acceptance (FR-015) without redelivery semantics that could replay an upload. Serial writes reduce contention on numbering.
- **Alternatives**: SQS (visibility-timeout redelivery is an automatic replay unless carefully neutralized); Step Functions (heavier than needed).

## R4 — Web-service-only data access (FR-018)
- **Decision**: One client (`client.py`) with three operations: `load` (read an IDO collection), `insert` (write a row), `invoke` (call an IDO method). No other ERP path exists in the code. All names that are not yet confirmed (SOAP operation names, collection names, method names, payload shapes) live in `idos.py` and the client's XML helpers.
- **Rationale**: Direction from the user; also removes a SQL driver, SQL credentials and a firewall path.
- **Alternatives**: Direct SQL for reads (rejected by FR-018); letting SyteLine assign numbers on insert (rejected by the clarified numbering answer; kept as the fallback if the routines are not callable).

## R5 — Pipeline
1. **Resolve customer** (read-only `load` on the customer-address collection, bill-to row only). Lookup rule: filter on the first word of the normalized name (`name LIKE '<FIRSTWORD>%'`, value escaped) plus, when supplied, the postal code; cap the result at 50 rows (a full cap is treated as ambiguous); then compare the full normalized name (upper-case, collapsed whitespace) and any supplied address fields in code. Exactly one distinct customer, else reject (nothing written, no number allocated). This avoids missing a customer whose stored name differs only in spacing.
2. **If live writes are disabled: stop here** with `LIVE_WRITES_DISABLED` (every `invoke` and `insert` is a write).
3. `invoke` `NextCoSp` → insert `SLCos`.
4. Per line: `invoke` `NextWOSp` → insert `CCT_Coitems` line (teardown value per the open item) → insert `CCTJobs` job.
5. **Read back** the CO, lines and jobs (`load`) and compare with what was sent.
6. Mark `succeeded` only if every row is verified.
- **Diffgram rule**: HTTP 200 is not acceptance. Insert responses are parsed for row-level errors.
- **Filter safety**: values placed in IDO filter strings are escaped (`'` doubled) by one helper in `idos.py`.

## R6 — Uncertainty classification (clarified FR-004)
- **Definite failure (retry allowed)**: failure before any write request was sent (validation, no/ambiguous customer match, login failure, live writes disabled, a number-allocation call that failed before any row exists), **or** the ERP rejected the very first insert and a successful read-back proves the row is absent.
- **Uncertain → `needs_reconciliation`**: any error/timeout/ambiguous response after an insert was sent where read-back cannot prove absence, any partial presence, or read-back failure. The job lists the CO number, line numbers and job numbers that may exist. No ERP delete, no automatic retry; a person resolves it.
- **Rationale**: Sheet §5 — the path is insert-only and timeouts can follow accepted inserts. A timeout never proves a row is absent.

## R7 — Idempotency
- **Decision**: `request_ref` → `upload_id` index with a conditional put in the same transaction as the job. Same `request_ref` and same payload hash → return the existing job (`duplicate:true`). Same `request_ref` with a different payload → 409. A job in `needs_reconciliation` is never replayed.

## R8 — Live-write gate (clarified FR-016)
- **Decision**: `SYTELINE_LIVE_WRITES` (default `false`), enforced inside `client.insert` and `client.invoke` (not only by callers). When false the worker performs only read-only steps. The dry-run health check sends no credentials and logs in to nothing. Discovery (T031) uses read-only calls only (WSDL inspection, `load`).

## R9 — Auth and attribution (clarified FR-017)
- **Decision**: `AWS_IAM` on both routes. Request carries `uploaded_by` (required); intake also records `requestContext.identity.userArn`. Only `confirmed: true` orders are accepted.

## R10 — Notes
- **Decision**: `add_note`/`get_notes` stay direct-invoke actions. `add_note`: `load` the record's `RowPointer` (CO from `SLCos`, job from `CCTJobs`), then `invoke` `CreateSpecificNoteSp` (creates and attaches the note; internal flag stored as `NoteFlag`) — a write, gated. `get_notes`: `load` `ObjectNotes` (`RefRowPointer` = the record's `RowPointer`) and `SpecificNotes` (joined on `SpecificNoteToken`), optionally `CreateDate >= since`.
- **Open**: which IDO exposes `CreateSpecificNoteSp` as a callable method.

## R11 — Health check
- **Decision**: Unchanged stages (`dns, tcp, wsdl, token`; dry run `load, dns, tcp, wsdl`). No SQL stage.

## Open items (do not block design; block live use)
1. **Method callability** (new, central): that `NextCoSp`, `NextWOSp` and `CreateSpecificNoteSp` can be invoked through the web service, and on which IDOs. If not, the numbering decision is revisited (fallback: SyteLine assigns numbers on insert).
2. **Customer collection** (new): the exact IDO and property names for the customer-address data; if none is exposed, FR-014 is revisited.
3. Customer matching rules: the sheet defers to the existing PO OCR resolver. Plan assumes the lookup rule in R5 step 1; confirm it with the resolver's owner.
4. Source and format of the per-line teardown value (treated as an optional pass-through string per line until confirmed).
5. Exact SOAP operation names and payload shapes, and the `CCT_Coitems`/`CCTJobs` required properties (the workbook lists `CCTJobs` required: Job, OrdType, Whse; `CCT_Coitems` is not in it).
6. `SLCos` fields filled from the customer match (analysis finding U1); retry path for `failed` jobs and a way to close `needs_reconciliation` jobs (I1, I2).
7. VPN/DNS/CIDR sign-offs listed in the instruction sheet.
