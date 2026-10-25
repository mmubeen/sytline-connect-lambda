# Research: SQL Server Calls for Numbers and Customer Lookup

## R1. Which SQL Server driver in Lambda
**Decision**: `python-tds` (module `pytds`, 1.17) with `pyOpenSSL` (+ `cryptography`) for TLS. Checked: all four install as cp313 Linux wheels with `pip --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64` (28 MB with `rapidfuzz`); `pytds.connect` takes `cafile`, `validate_host`, `timeout`, `login_timeout`, `autocommit`; its errors include `LoginError`, `ClosedConnectionError`, `TimeoutError`, `DatabaseError`, `InterfaceError`.
**Rationale**: pure Python, no native libraries, fits the layer build the repo already uses. The demo's `pyodbc` needs unixODBC and Microsoft's ODBC driver (native, licensed, container image).
**Known limits (verified in the driver source)**: (a) TLS 1.2 only. (b) Without a `cafile` the driver **does not encrypt** (`ENCRYPT_NOT_SUP`): that is exactly the explicit unencrypted option of R4, and it must never be reached by accident (a missing CA file is a refusal, not a fallback). (c) With a `cafile` the certificate chain is always verified; only the host-name check can be switched off (`validate_host=False`). So "verification off" in the spec is realised as "host-name check off", and a self-signed test server is handled by trusting its own certificate as the CA file. (d) No TDS 8 / strict encryption (SQL Server 2022 strict mode); plain TLS-in-TDS works on 2016 to 2022.
**Alternatives**: `pyodbc` + `msodbcsql18` (above); `pymssql` (bundled FreeTDS, weaker certificate control, wheel availability for 3.13 uncertain).

## R2. Route per operation
**Decision**: three operations (`next_co`, `next_job`, `customer`) plus `defaults`, each with one setting that already exists or is added alongside: `SYTELINE_NEXT_CO_IDO`, `SYTELINE_NEXT_JOB_IDO`, `SYTELINE_CUSTOMER_IDO` (existing names in `idos.py`) and `SYTELINE_DEFAULTS_IDO` (new). Rule in `routes.py`:
- variable **absent**: the existing default web-service name (so deployments and tests that never set it behave as before); for `defaults`, which had no earlier behaviour, absent means **off**: no defaults are read and nothing connects;
- variable **set to a name**: web service, that endpoint;
- variable **set to `database` or empty**: database.
The template always sets all four, with default `database` (the existing web-service names were guesses, unconfirmed, so no endpoint is known to exist). The decision reads settings only; nothing is probed and no failure changes it.
**Rationale**: FR-012 and the clarified rule. An absent variable keeps old stacks unchanged. Accepting both `database` and empty avoids relying on a platform accepting an empty environment value.
**Migration note**: updating an existing VPC stack makes its numbering and customer lookup use the database; without database settings in the secret they fail clearly (`DATABASE_NOT_CONFIGURED`) before anything is reserved. Setting a parameter to the old IDO name restores the web-service route.

## R3. Reserving numbers (demo behaviour, adapted to the driver)
**Decision**: per number, one batch `SET NOCOUNT ON; BEGIN TRANSACTION; DECLARE @Key AS LongList; DECLARE @Infobar AS Infobar; EXEC [dbo].[<routine>] NULL, NULL, %s, @Key OUTPUT, @Infobar OUTPUT; SELECT @Key AS key_val, @Infobar AS infobar;`, parse the first integer from the key (LongList values come back as `123`, `^123^`, `^123,124^`), then a separate `COMMIT TRANSACTION`; on any problem before the commit, `IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION`. The connection is `autocommit=True` so the explicit transaction spans the two statements. Site id default 10 (`SYTELINE_SITE_ID`). One connection serves a whole reservation (CO + all jobs) and is closed in a `finally`.
**Error classification** (FR-002, FR-003): connect or login failure, or a failure before the batch was sent: `ErpUnreachable` (nothing happened). A SQL error from the routine, an empty row or an unreadable key: rolled back, `ErpRejected` with the routine's name and message. A dropped connection, timeout or driver error **after the batch was sent or during the commit**: `ErpUncertain`, never retried, number treated as consumed. Infobar text is logged as a warning (as in the demo) and not treated as failure by itself.
**UNCONFIRMED**: the `LongList` / `Infobar` types and the routine signatures are taken from the demo; verify read-only against the plant database before the first live run.

## R4. TLS and certificate handling (clarified 2026-10-22: encrypted and verified by default; explicit unencrypted option)
**Decision**: one mode switch, `SYTELINE_SQL_ENCRYPT` (stack parameter `SqlEncrypt`, default `true`), decided before any connection:
- **encrypted (default; anything but the exact value `false`)**: the CA file is **required**: secret key `SQL_CA_PEM` (PEM text, may hold several certificates) is written to a mode-0600 file under `/tmp` and passed as `cafile`; a missing CA is refused with `DATABASE_NOT_CONFIGURED` and **never** falls back to unencrypted. The host name is checked against the certificate by default; `SYTELINE_SQL_VERIFY_HOSTNAME=false` turns only that check off and warns on every connection.
- **unencrypted (only when `SYTELINE_SQL_ENCRYPT` is exactly `false`)**: no `cafile` is passed (the driver then does not encrypt), `SQL_CA_PEM` is not required, the host-name setting is irrelevant. Every connection logs a WARNING ("database connection is UNENCRYPTED: the login and password cross the network unprotected"), the connectivity check reports it (R8), and nothing else enables it: no error, timeout or failed handshake retries without TLS.
**Rationale**: the plant server does not present a certificate the functions can verify and the demo connects with `Encrypt=no`; the spec lets the deployer accept that explicitly, as the web service's plain-HTTP override does. The safe mode stays the default.
**Operational note**: a SQL Server set to *Force Encryption* refuses an unencrypted client; the deployer must then supply a CA (encrypted mode) or turn Force Encryption off. In the unencrypted mode the driver's login password is only obfuscated, not protected.
**Alternatives**: encrypt without verifying the certificate (the driver cannot do this: it always verifies against the CA file); require a CA always (rejected by the deployer).

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
**Decision**: `health` adds `"routes": {"next_co": "database", ...}` (always) and, outside the credential-free dry run, when any operation uses the database, the stages `db_dns` (server name resolves), `db_tcp` (port accepts a connection), `db_login` (TLS handshake and certificate check when encrypted, login, then `SELECT 1`), each reported separately, with the detail saying whether the connection is `encrypted` or `UNENCRYPTED` and a top-level `warnings` entry when it is not; a certificate failure says so in plain words and never includes the password. The dry run needs no credentials, so it reports routes only. Existing stages and the `ok` rule are unchanged apart from `ok` also requiring the database stages when they run.

## R9. Network and template
**Decision**: in create-network mode add egress rules for TCP 1433 to the plant CIDRs (`EgressPlantSql1..3`, conditioned like the existing HTTP rules); in existing-network mode the deployer's security group must allow it (documented). Four route parameters (default `database`) feed environment variables on `ReserveFunction`, `WorkerFunction` and `DirectFunction`. No new function and no new permission: the secret read already exists.

## R10. Live writes and the one-step upload
**Decision**: the database numbering path calls `config.live_writes_enabled()` before connecting, exactly like the web-service path (FR-004). `pipeline.Upload` (feature 001) and `reserve.run_reserve` (feature 004) both obtain numbers from `NumberSource`; both map `ErpUncertain` to `UNCERTAIN_OUTCOME` (parked / reported), which the web-service path previously mapped to `ERP_UNREACHABLE`.

## R11. Guard tests
**Decision**: `tests/unit/test_no_sql.py` changes from "no SQL client anywhere" to: the driver may be imported only in `src/syteline/sqlserver.py`; no other SQL client module is imported anywhere; and a recording-connection audit (SC-005) runs every code path and asserts each executed statement matches the whitelist (the two `SELECT`s, the two `EXEC`s, `COMMIT`/`ROLLBACK`, `SELECT 1`) with no `INSERT`/`UPDATE`/`DELETE`/`MERGE`/`DROP`/`ALTER`/`CREATE`/`TRUNCATE`.

## Revision 2026-10-22: deploy message wording (R26)

## R26. One message reworded, nothing else
**Decision**: in `src/deploy.sh` the line in `update_secret` becomes `Will add or update these keys in the credentials secret: <keys>. All other keys stay as they are, and values are never printed.` It keeps its place, its trigger (database values present) and the existing confirmation question and `--yes`; no value is ever interpolated except key names.
**Test**: the deploy-script tests assert the new text (the notice naming the keys and "All other keys stay as they are"), and that a database password given in the settings file never appears in stdout or stderr.
**Alternatives**: a pre-deploy network check or one combined summary (offered in the clarification, not chosen).

## Revision 2026-10-22: database in the diagnostics (R27 to R30)

## R27. Who decides, and what `run_diagnostics` receives
**Decision**: `health.run_health` computes one boolean, `database_probe = not dry_run and (routes.uses_database())` (the same expression that decides the database stages) and passes it as `run_diagnostics(cfg, timeout, env=None, database=False)`. `diagnostics.py` never imports routes, so it stays free of route logic. A false value adds no keys, so existing diagnostics output and tests are unchanged.
**Rationale**: one decision, one place; the diagnosis can never probe a host the health check would not test (spec FR-019).
**Alternatives**: let `diagnostics` call `routes`/`config` itself (duplicates the rule); probe whenever `SQL_SERVER` exists (rejected in the clarification).

## R28. What is probed and reported
**Decision**: when `database` is true and `cfg["SQL_SERVER"]` is set, `run_diagnostics` adds `d["database"] = probe_host(server, port, probe_timeout)` (same shape as `erp`: lookup attempts, `gethostbyname` on failure, up to `MAX_ADDRESSES` connections, `connected`), and `d["dns_raw"]["database"] = raw_probe(server, nameservers)` when a nameserver is known. The port is `SQL_PORT` (default 1433; a non-numeric value is reported as skipped, not raised). Nothing else is sent: no TLS handshake, no login, no credential, no query.
**Skipped, with a reason** (key `d["database_skipped"]`): the health call was a dry run ("a dry run does not read the secret, which holds the database server name"); the secret could not be read ("the credentials secret could not be read"); the secret has no `SQL_SERVER` ("the secret holds no SQL_SERVER"). When `database` is false nothing is added (no note), as for the health stages.
**Consequence to state**: the module's promise changes from "reads no secret" to "uses the server name and port the health check has already read from the secret; sends no credentials". Updated in the module docstring and the docs.

## R29. Database findings
**Decision**: a new `database_findings(d)` appended to `findings`, one entry per case: (a) name does not resolve, with the nameserver's own answer used as for the ERP (NXDOMAIN, SERVFAIL/REFUSED, no answer, answers correctly but the function's lookup fails); (b) name resolves but no address accepts a connection on the port while the AWS endpoint is reachable (route, security group on that port, SQL Server not listening or its firewall); (c) name resolves and a connection succeeds ("the database host and port are reachable: if db_login still fails, the cause is the login, the certificate or the encryption setting; see that stage's error"). The ERP findings are unchanged and the database entries name "the database".
**Alternatives**: reuse the ERP finding text with a different noun (rejected: the remedies differ, e.g. port 1433 and the SQL security group).

## R30. Where the decision is passed
**Decision**: the four `run_diagnostics` call sites in `health.py` pass `database=` as follows: the normal path passes `database_probe`; the secret-store-failure path, the missing-config path and the HTTPS-refused path pass `database=False` and the result carries no database note except the secret-store-failure case, which sets `database_skipped` itself ("the credentials secret could not be read") when `database_probe` would otherwise have been true. The dry-run path sets `database_skipped` with the dry-run reason when the rule (ignoring dry run) would have probed.

