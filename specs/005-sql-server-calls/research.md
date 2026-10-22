# Research: SQL Server Calls for Numbers and Customer Lookup

## R1. Which SQL Server driver in Lambda
**Decision**: `python-tds` (module `pytds`, 1.17) with `pyOpenSSL` (+ `cryptography`) for TLS. Checked: all four install as cp313 Linux wheels with `pip --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64` (28 MB with `rapidfuzz`); `pytds.connect` takes `cafile`, `validate_host`, `timeout`, `login_timeout`, `autocommit`; its errors include `LoginError`, `ClosedConnectionError`, `TimeoutError`, `DatabaseError`, `InterfaceError`.
**Rationale**: pure Python, no native libraries, fits the layer build the repo already uses. The demo's `pyodbc` needs unixODBC and Microsoft's ODBC driver (native, licensed, container image).
**Known limits (verified in the driver source)**: (a) TLS 1.2 only. (b) Without a `cafile` the driver **does not encrypt** (`ENCRYPT_NOT_SUP`), so we never connect without one (R4). (c) With a `cafile` the certificate chain is always verified; only the host-name check can be switched off (`validate_host=False`). So "verification off" in the spec is realised as "host-name check off", and a self-signed test server is handled by trusting its own certificate as the CA file. (d) No TDS 8 / strict encryption (SQL Server 2022 strict mode); plain TLS-in-TDS works on 2016 to 2022.
**Alternatives**: `pyodbc` + `msodbcsql18` (above); `pymssql` (bundled FreeTDS, weaker certificate control, wheel availability for 3.13 uncertain).

## R2. Route per operation
**Decision**: three operations (`next_co`, `next_job`, `customer`) plus `defaults`, each with one setting that already exists or is added alongside: `SYTELINE_NEXT_CO_IDO`, `SYTELINE_NEXT_JOB_IDO`, `SYTELINE_CUSTOMER_IDO` (existing names in `idos.py`) and `SYTELINE_DEFAULTS_IDO` (new). Rule in `routes.py`:
- variable **absent**: the existing default web-service name (so deployments and tests that never set it behave as before); for `defaults`, which had no earlier behaviour, absent means **off**: no defaults are read and nothing connects;
- variable **set to a name**: web service, that endpoint;
- variable **set to `database` or empty**: database.
The template always sets all four, with default `database` (the existing web-service names were guesses, unconfirmed, so no endpoint is known to exist). The decision reads settings only; nothing is probed and no failure changes it.
**Rationale**: FR-012 and the clarified rule. An absent variable keeps the stand-alone version and old stacks unchanged. Accepting both `database` and empty avoids relying on a platform accepting an empty environment value.
**Migration note**: updating an existing VPC stack makes its numbering and customer lookup use the database; without database settings in the secret they fail clearly (`DATABASE_NOT_CONFIGURED`) before anything is reserved. Setting a parameter to the old IDO name restores the web-service route.

## R3. Reserving numbers (demo behaviour, adapted to the driver)
**Decision**: per number, one batch `SET NOCOUNT ON; BEGIN TRANSACTION; DECLARE @Key AS LongList; DECLARE @Infobar AS Infobar; EXEC [dbo].[<routine>] NULL, NULL, %s, @Key OUTPUT, @Infobar OUTPUT; SELECT @Key AS key_val, @Infobar AS infobar;`, parse the first integer from the key (LongList values come back as `123`, `^123^`, `^123,124^`), then a separate `COMMIT TRANSACTION`; on any problem before the commit, `IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION`. The connection is `autocommit=True` so the explicit transaction spans the two statements. Site id default 10 (`SYTELINE_SITE_ID`). One connection serves a whole reservation (CO + all jobs) and is closed in a `finally`.
**Error classification** (FR-002, FR-003): connect or login failure, or a failure before the batch was sent: `ErpUnreachable` (nothing happened). A SQL error from the routine, an empty row or an unreadable key: rolled back, `ErpRejected` with the routine's name and message. A dropped connection, timeout or driver error **after the batch was sent or during the commit**: `ErpUncertain`, never retried, number treated as consumed. Infobar text is logged as a warning (as in the demo) and not treated as failure by itself.
**UNCONFIRMED**: the `LongList` / `Infobar` types and the routine signatures are taken from the demo; verify read-only against the plant database before the first live run.

## R4. TLS and certificate handling (clarified: always encrypted, verified by default)
**Decision**: the CA file is **required**: the secret key `SQL_CA_PEM` (PEM text, may hold several certificates) is written to a mode-0600 file under `/tmp` and passed as `cafile`; with no CA configured the connection is refused with `DATABASE_NOT_CONFIGURED` rather than connecting unencrypted. The host name is checked against the certificate by default; `SYTELINE_SQL_VERIFY_HOSTNAME=false` (explicit) turns only that check off and logs a warning on every connection. A self-signed test server: put its certificate in `SQL_CA_PEM`.
**Rationale**: matches the spec's wording within what the driver can do (R1); encryption cannot be turned off.

## R5. Credentials and settings
**Decision**: in the existing ERP secret (clarified): `SQL_SERVER`, `SQL_DATABASE`, `SQL_USER`, `SQL_PASS`, optional `SQL_PORT` (default 1433) and `SQL_CA_PEM`. `config.load_config` returns them (the secret's keys are filtered to known names today). Environment fallbacks exist for local tests only (never for the password in a deployment). Nothing of these is logged: only server name, database name, port and driver outcome.
**Consequence accepted in the spec**: every function that reads the secret can read the database settings.

## R6. Customer search (port of the demo)
**Decision**: `customer_match.py` ports the demo's `_name_tokens` (lowercase, drop words under 3 characters and the stop-word list, at most 5 tokens), `_zip_token` (5-digit ZIP), `_normalize` (punctuation, street abbreviations), candidate-name/address building, `_score` (name: max of `token_set_ratio` and `partial_ratio`; address: 0.6 token-set + 0.4 partial; combined 50/50, falling back to whichever side exists) and the decision (`match` if best ≥ 70 and gap to the runner-up ≥ 3; `ambiguous` if best ≥ 70 but the gap is smaller; `low_confidence` below 70; not found with no candidates). The candidate query is parameterised: `SELECT TOP 1000 cust_num, cust_seq, name, [addr##1..4], city, state, zip, country FROM custaddr WHERE LOWER(name) LIKE %s OR ... OR zip LIKE %s ORDER BY cust_num, cust_seq`. The address text is the caller's address lines, city, state and postal code joined (the demo's free-text ship-to address). Thresholds from `SYTELINE_MATCH_MIN_SCORE` (70), `SYTELINE_MATCH_MARGIN` (3), `SYTELINE_MATCH_CAP` (1000). Only `match` is accepted; the others refuse with the best candidates and scores (new `CUSTOMER_LOW_CONFIDENCE`; existing `CUSTOMER_AMBIGUOUS`, `CUSTOMER_NOT_FOUND`).
**SC-003 evidence**: a golden file of at least 30 cases generated by running the demo's own functions on a synthetic address table (`tests/tools/make_customer_golden.py`), and a test that this module reproduces each decision and score.
**Note**: the web-service resolver of feature 001 (exact name match) stays for when `SYTELINE_CUSTOMER_IDO` names an endpoint.

## R7. Customer defaults (terms, ship-via)
**Decision**: `defaults.py`. Database route: the demo's two queries on the `co` table (`SELECT TOP 1 terms_code, ship_code FROM co WHERE LTRIM(RTRIM(cust_num)) = %s AND cust_seq = %s ORDER BY co_num DESC`, then without the sequence), first hit with a code wins. Web-service route (when `SYTELINE_DEFAULTS_IDO` names an endpoint): load `TermsCode, ShipCode, CoNum` for the customer from that collection and pick the highest `CoNum` client-side. Both return `{terms_code, ship_code, source}` or `None`. Applied to the CO header in the save (`TermsCode`, `ShipCode` on `SLCos`, UNCONFIRMED names) and in the one-step upload. A failure or no previous order never blocks the save: the header goes without them and a `warnings` entry says why (stored on the reservation and shown by the status view).

## R8. Connectivity check
**Decision**: `health` adds `"routes": {"next_co": "database", ...}` (always) and, outside the credential-free dry run, when any operation uses the database, the stages `db_dns` (server name resolves), `db_tcp` (port accepts a connection), `db_login` (TLS handshake, certificate check and login, then `SELECT 1`), each reported separately; a certificate failure says so in plain words and never includes the password. The dry run needs no credentials, so it reports routes only. Existing stages and the `ok` rule are unchanged apart from `ok` also requiring the database stages when they run.

## R9. Network and template
**Decision**: in create-network mode add egress rules for TCP 1433 to the plant CIDRs (`EgressPlantSql1..3`, conditioned like the existing HTTP rules); in existing-network mode the deployer's security group must allow it (documented). Four route parameters (default `database`) feed environment variables on `ReserveFunction`, `WorkerFunction` and `DirectFunction`. No new function and no new permission: the secret read already exists.

## R10. Live writes and the one-step upload
**Decision**: the database numbering path calls `config.live_writes_enabled()` before connecting, exactly like the web-service path (FR-004). `pipeline.Upload` (feature 001) and `reserve.run_reserve` (feature 004) both obtain numbers from `NumberSource`; both map `ErpUncertain` to `UNCERTAIN_OUTCOME` (parked / reported), which the web-service path previously mapped to `ERP_UNREACHABLE`.

## R11. Guard tests
**Decision**: `tests/unit/test_no_sql.py` changes from "no SQL client anywhere" to: the driver may be imported only in `src/syteline/sqlserver.py`; no other SQL client module is imported anywhere; and a recording-connection audit (SC-005) runs every code path and asserts each executed statement matches the whitelist (the two `SELECT`s, the two `EXEC`s, `COMMIT`/`ROLLBACK`, `SELECT 1`) with no `INSERT`/`UPDATE`/`DELETE`/`MERGE`/`DROP`/`ALTER`/`CREATE`/`TRUNCATE`.

## R12. Stand-alone version
**Decision**: unchanged and out of scope. Its template sets none of the route variables, so the absent-variable rule keeps its web-service behaviour; the database code is in the zip but unreachable without settings.
