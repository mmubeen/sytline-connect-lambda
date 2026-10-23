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
**Rationale**: FR-012 and the clarified rule. An absent variable keeps the stand-alone version and old stacks unchanged. Accepting both `database` and empty avoids relying on a platform accepting an empty environment value.
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

## R12. Stand-alone version
**Decision**: unchanged and out of scope. Its template sets none of the route variables, so the absent-variable rule keeps its web-service behaviour; the database code is in the zip but unreachable without settings.


## Revision 2026-10-22: the stand-alone health check (R13 to R15)

## R13. What the stand-alone version gets
**Decision**: only the connectivity test. The stand-alone function has just the `health`, `add_note` and `get_notes` actions, so no number reservation, customer search or defaults exist there to route. It reuses `health._database_stages` (`db_dns`, `db_tcp`, `db_login`) and `sqlserver.SqlServer` unchanged, with the same encryption rules (encrypted and verified by default, `SYTELINE_SQL_ENCRYPT=false` the explicit warned option, no CA then needed) and the same credential rules (keys only in the secret; the stand-alone role already reads that secret).
**Rationale**: matches the clarified spec (FR-011, assumptions); no new code path that can write.

## R14. Switching the check on, and attachment
**Decision**: the route variables are not set in the stand-alone template (absent = earlier behaviour, and none of its actions use the database), so `routes.uses_database()` is false there. A new setting `SYTELINE_DB_CHECK` (stand-alone parameter `DatabaseCheck`, default `false`) turns the database stages on explicitly. The template also sets `SYTELINE_ATTACHED` to `true` only when all three attachment settings are supplied (the existing `AttachToVpc` condition). Rule in `health.run_health`: run the database stages when not a dry run and (`routes.uses_database()` or `config.db_check()`); when `db_check()` is on but the function is not attached, do not run them and add `"database_check": "skipped: the function is not attached to a VPC"` to the response (a route to the plant database cannot exist outside a VPC). The plant version is unchanged: it never sets `SYTELINE_DB_CHECK`.
**Alternatives**: setting the four route variables to `database` in the stand-alone template (misleading: no action uses them, and `routes` would claim the database serves operations it does not); always running the stages when attached (changes the default health result of existing attached deployments, and fails them where the database settings are not yet in the secret).

## R15. Template, deploy and network
**Decision**: `template-standalone.yaml` gains parameters `DatabaseCheck` (default `false`), `SqlEncrypt` (default `true`), `SqlVerifyHostname` (default `true`), `SqlLoginTimeout` (default `5`) and the env on `DirectFunction`: `SYTELINE_DB_CHECK`, `SYTELINE_SQL_ENCRYPT`, `SYTELINE_SQL_VERIFY_HOSTNAME`, `SYTELINE_SQL_LOGIN_TIMEOUT`, `SYTELINE_ATTACHED`. A rule refuses `DatabaseCheck=true` without the three attachment settings (nothing is created). `deploy.sh --standalone` and `deploy.standalone.env.example` get `DATABASE_CHECK`, `SQL_ENCRYPT`, `SQL_VERIFY_HOSTNAME`, `SQL_LOGIN_TIMEOUT` (always passed, validated like the plant ones; `SQL_ENCRYPT=false` warns). The database keys go in the secret (`SQL_SERVER`, `SQL_DATABASE`, `SQL_USER`, `SQL_PASS`, optional `SQL_PORT`, `SQL_CA_PEM` while encrypted). The stack creates no network rule: the deployer's security group must allow TCP 1433 from the function to the database. The layer already contains the driver (shared `requirements.txt`).


## Revision 2026-10-22: automatic stand-alone check (R16)

## R16. Three-valued database check
**Decision**: `SYTELINE_DB_CHECK` (stand-alone parameter `DatabaseCheck`) takes `auto`, `true` or `false`; absent or any other value means `auto`. `config.db_check_mode()` returns the normalised value (trimmed, lower-cased). Rule in `health.run_health` (not a dry run, which never touches the database):
- the plant routes decide first, exactly as today: `routes.uses_database()` runs the stages whatever the mode (the plant template never sets `SYTELINE_DB_CHECK` or `SYTELINE_ATTACHED`);
- otherwise, mode `false`: no stages and no note; mode `auto`: run the stages when `config.attached()`, else do nothing (quietly: it is the default for functions that were never meant to reach the plant, so no skip note); mode `true`: run when attached, else no stages and the note `database_check: skipped: the function is not attached to a VPC` (reachable only by forcing the environment: the template and `deploy.sh` refuse `true` without the attachment).
A missing database key is not special-cased: `db_login` fails naming the keys, as for the plant version, and `ok` is false.
**Consequence to state**: an already-attached stand-alone deployment starts showing the three stages on its next health call, and `ok` is false until the database keys exist in the secret; `DatabaseCheck=false` keeps the earlier result.
**Alternatives**: auto only when `SQL_SERVER` is in the secret (rejected by the deployer: a missing key should be visible, not silent); keep default off (superseded).


## Revision 2026-10-22: parity tests for the two deployment modes (R17 to R19)

## R17. Scope: tests only
**Decision**: no template, script or code restructuring (clarified). A new `tests/unit/test_mode_parity.py` compares the plant (`src/template.yaml`) and stand-alone (`src/template-standalone.yaml`) deployments, `src/deploy.sh` and the two example files. Findings when planned: the eight parameters present in both templates (`LiveWrites`, `SqlEncrypt`, `SqlLoginTimeout`, `SqlVerifyHostname`, `SytelineConfig`, `SytelineSecretArn`, `SytelineUser`, `UseFipsEndpoints`) are identical today apart from description wording, and every shared environment variable is wired the same way except `SYTELINE_WSDL` (plant: one parameter; stand-alone: the https/http pair by design). The tests lock that state in.

## R18. What "shared" and "in sync" mean
**Decision**: the shared set is computed, not listed: the parameters that exist in both templates. Adding a parameter to both therefore puts it under test automatically; adding it to only one is not an error (modes legitimately differ: network, attachment, route and tuning settings are plant-only; WSDL pair, `AllowPlainHttp`, `VerifyTls`, `Attach*`, `DatabaseCheck` stand-alone-only). For each shared parameter the tests check:
1. **Template definition**: `Type`, `Default`, `AllowedValues`, `AllowedPattern` equal in both (descriptions may differ: they describe the mode).
2. **Function wiring**: the environment variables of `DirectFunction` that reference the parameter are the same names in both templates (`{Ref: Param}` only; derived values such as the WSDL `If` are listed in an explicit exceptions table with a reason).
3. **deploy.sh**: the parameter is passed in both the plant branch and the stand-alone branch, from the same environment variable (the upper-snake-case of the parameter name: `SqlLoginTimeout` -> `SQL_LOGIN_TIMEOUT`, with `UseFipsEndpoints` -> `USE_FIPS_ENDPOINTS`), and where the script supplies a default (`${VAR:-default}`) it equals the template default. A small mapping table lists parameters whose script variable does not follow the rule (none expected today).
4. **Example files**: both `deploy.env.example` and `deploy.standalone.env.example` contain a line `VAR=` for that variable (commented or not), and for parameters with a non-empty template default the example value equals it.
**Exceptions table**: `SYTELINE_WSDL` wiring only. Every entry must carry a reason and the test fails if an entry is no longer needed (no stale exceptions).

## R19. Proving the tests catch drift (SC-011)
**Decision**: besides checking the real files, the module has a self-test that mutates in-memory copies (a changed default, a changed allowed value, a parameter missing from `deploy.sh` text, a variable missing from an example, a differently named environment variable) and asserts the checker reports each, so a future edit that weakens the checker is itself caught. The checker is a set of plain functions returning lists of violation strings; the real-file tests assert the lists are empty.
**Alternatives**: snapshot/golden files of the parameter tables (hide intent, produce noisy diffs); generating one file from the other (structural change, rejected by the clarification).


## Revision 2026-10-22: single settings file (R20 to R23)

## R20. One file for both modes
**Decision**: `deploy.env` and `deploy.env.example` serve `./deploy.sh` and `./deploy.sh --standalone` (clarified, FR-017). `env_file` is always `deploy.env`; `--standalone` still selects the stack, template and build directory (`--config-env standalone`). `deploy.standalone.env.example` is deleted. The example is laid out as: (1) shared settings, written once (ERP address, user, secret ARN, config, AWS profile, live-writes gate, FIPS, database login block, `SQL_ENCRYPT`, `SQL_VERIFY_HOSTNAME`, `SQL_LOGIN_TIMEOUT`); (2) a `PLANT ONLY` block (`WORKER_*`, network creation, `API_*`, `IDP_*`, route IDOs, `SITE_ID`, `SQL_TIMEOUT`, `MATCH_*`, default warehouse/order type); (3) a `STAND-ALONE ONLY` block (`ATTACH_*`, `ALLOW_PLAIN_HTTP`, `VERIFY_TLS`, `DATABASE_CHECK`).
**Rationale**: a setting both modes share has one name, one default and one place; the parity tests no longer need to compare two example files.
**Alternatives**: a common file loaded by two mode files (rejected in the clarification: three files); keep two files with a drift check (rejected).

## R21. A shared value is shared
**Decision**: a shared setting has one value for both stacks (for example the same `SYTELINE_WSDL`). Where the modes would differ, the setting is not shared but mode-only. Nothing is renamed. Consequence stated in the example: the stand-alone mode still refuses an `http://` ERP address unless `ALLOW_PLAIN_HTTP=true`, so a plant address that is plain HTTP needs that explicit stand-alone-only opt-in.
**Rationale**: no hidden per-mode overrides; the file reads the same whichever mode runs.

## R22. Each mode ignores the other's block
**Decision**: the stand-alone branch of `deploy.sh` never reads plant-only variables and the plant branch never reads stand-alone-only variables; a value present in the other block is neither validated nor passed. The "missing" error text names `deploy.env` in both branches. The database-login merge (`update_secret`) is unchanged and runs from the shared block in both modes.

## R23. The retired file is refused, not read
**Decision**: when `--standalone` is used and `deploy.standalone.env` exists, `deploy.sh` exits before building with a message that names the file and says to move its settings into `deploy.env` (stand-alone-only block) and delete it. No silent fallback to the old file. The `.gitignore` entry for `src/deploy.standalone.env` stays so a leftover file is never committed.
**Alternatives**: read the old file for a transition period (rejected: two sources of truth, the problem being solved).

## R24. Parity tests follow the new layout
**Decision**: `tests/unit/test_mode_parity.py` changes the example check only: the single example must contain `VAR=` for every shared variable (its default equal to the template default) in the shared part, every plant-only variable only in the `PLANT ONLY` block and every stand-alone-only variable only in the `STAND-ALONE ONLY` block; the script check adds that each branch references no variable of the other mode's block. The self-test mutates copies to prove each rule fires (misplaced variable, missing shared variable, cross-mode read). `test_deploy_script.py` gains: `--standalone` reads `deploy.env`; leftover `deploy.standalone.env` stops it.

## R25. The stand-alone refusal of plant network settings is replaced by a notice
**Decision**: today `--standalone` stops with "not applicable to the stand-alone version" when a plant network setting (`WORKER_*`, `HOST_NETWORK_CIDR`, `PLANT_CIDR_*`, `DYNAMODB_PREFIX_LIST_ID`, `API_VPCE_ID`, `CREATE_API_ENDPOINT`, `CREATE_VPC_ENDPOINTS`) is set. With one shared file those values are normal, so the refusal is removed: the settings are not passed to CloudFormation and one line names them ("ignored by the stand-alone version: ..."). Consistent with R22 and with the plant branch's existing "ignored" warning for create-mode settings.
**Consequence**: two tests that expect the refusal (`test_deploy_script.py`, "not applicable") change to expect the notice and no pass-through.

## Revision 2026-10-22: deploy message wording (R26)

## R26. Two messages reworded, nothing else
**Decision**: in `src/deploy.sh` the attached stand-alone line becomes `Check: the subnets need a route to the ERP and a way to reach Secrets Manager (VPC endpoint or NAT). The stack does not create either. See docs/STANDALONE.md.` and the line in `update_secret` becomes `Will add or update these keys in the credentials secret: <keys>. All other keys stay as they are, and values are never printed.` Both keep their place, their trigger (attached deploy; database values present) and the existing confirmation question and `--yes`; no value is ever interpolated except key names.
**Test**: the deploy-script tests assert the new text (both `Check:` naming the ERP route, Secrets Manager and `docs/STANDALONE.md`; the notice naming the keys and "All other keys stay as they are"), and that a database password given in the settings file never appears in stdout or stderr.
**Alternatives**: a pre-deploy network check or one combined summary (offered in the clarification, not chosen).

