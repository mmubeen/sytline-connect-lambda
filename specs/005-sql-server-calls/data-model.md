# Data Model: SQL Server Calls for Numbers and Customer Lookup

Nothing new is stored: the database is read (and, for numbering, driven through SyteLine's own routines) and results flow into the existing reservation and upload records.

## Operation Route (setting, not stored data)
| Operation | Setting | Web-service value | Database value | Absent |
|---|---|---|---|---|
| Reserve the CO number | `SYTELINE_NEXT_CO_IDO` | the IDO that exposes `NextCoSp` | `database` or empty | legacy default name |
| Reserve job numbers | `SYTELINE_NEXT_JOB_IDO` | the IDO that exposes `NextWOSp` | `database` or empty | legacy default name |
| Find the customer | `SYTELINE_CUSTOMER_IDO` | the customer collection | `database` or empty | legacy default name |
| Read customer defaults | `SYTELINE_DEFAULTS_IDO` | the order collection | `database` or empty | **off**: no defaults are read |
`routes.route(operation)` returns `("webservice", name)`, `("database", None)` or, for an unset `defaults` setting only, `("off", None)`; it reads only the settings above (FR-012). The template always sets all four (default `database`).

## Database Connection Settings (in the ERP credentials secret)
| Key | Meaning | Rule |
|---|---|---|
| `SQL_SERVER` | host name (must match the certificate unless host-name checking is switched off or the connection is unencrypted) | required for any database route |
| `SQL_DATABASE` | database name | required |
| `SQL_USER`, `SQL_PASS` | login | required; never logged or returned |
| `SQL_PORT` | TCP port | optional, default 1433 |
| `SQL_CA_PEM` | PEM text of the certificate(s) to trust | **required in the default (encrypted) mode**: no connection is made without it, and its absence never makes the connection unencrypted; not needed when `SYTELINE_SQL_ENCRYPT=false` |
Environment: `SYTELINE_SQL_ENCRYPT` (default `true`; exactly `false` = the explicit unencrypted option: no CA needed, warning on every connection, reported by the connectivity check), `SYTELINE_SITE_ID` (default 10), `SYTELINE_SQL_VERIFY_HOSTNAME` (`false` turns the host-name check off, warns each time), `SYTELINE_SQL_LOGIN_TIMEOUT` (5 s), `SYTELINE_SQL_TIMEOUT` (10 s), `SYTELINE_MATCH_MIN_SCORE` (70), `SYTELINE_MATCH_MARGIN` (3), `SYTELINE_MATCH_CAP` (1000).

## Reserved Number
Returned by `NumberSource.next_co()` / `next_job()` as a string of digits. Created by a committed routine call; never reused. Outcomes: the number; `ErpRejected` (routine error, nothing consumed as far as is known); `ErpUncertain` (a number may be consumed); `ErpUnreachable` (nothing happened).

## Customer Candidate
`{cust_num, cust_seq, name, addr1..addr4, city, state, zip, country}` from `custaddr`; plus `score` (0 to 100, one decimal after rounding). At most `SYTELINE_MATCH_CAP` rows per search.

## Customer Match
`{status: match|ambiguous|low_confidence|not_found, cust_num?, cust_seq?, matched_name?, score?, runner_up?: {cust_num, cust_seq, name, score}}`. Only `match` is accepted (`cust_num` and `cust_seq` then feed the reservation's `customer_match`). Mapping to errors: `ambiguous` -> `CUSTOMER_AMBIGUOUS`, `low_confidence` -> `CUSTOMER_LOW_CONFIDENCE`, `not_found` -> `CUSTOMER_NOT_FOUND`; the error response includes the best candidates and their scores (nothing else of the customer rows).

## Customer Defaults
`{terms_code, ship_code, source: "same_ship_to"|"any_order"}` or `None` (no previous order or unreadable). Trimmed strings. Written to the CO header as `TermsCode` / `ShipCode` when present.

## Reservation record (feature 004) additions
`warnings`: list of strings (for example "customer defaults not available: ..."), set by the save run, shown by the status view. No other field changes.

## Statement whitelist (the only SQL the driver may run)
1. `SELECT TOP <n> cust_num, cust_seq, name, [addr##1], ... FROM custaddr WHERE <name LIKE / zip LIKE terms, parameterised> ORDER BY cust_num, cust_seq`
2. `SELECT TOP 1 terms_code, ship_code FROM co WHERE LTRIM(RTRIM(cust_num)) = %s [AND cust_seq = %s] ORDER BY co_num DESC`
3. `SET NOCOUNT ON; BEGIN TRANSACTION; DECLARE @Key AS LongList; DECLARE @Infobar AS Infobar; EXEC [dbo].[NextCoSp|NextWOSp] NULL, NULL, %s, @Key OUTPUT, @Infobar OUTPUT; SELECT @Key AS key_val, @Infobar AS infobar;`
4. `COMMIT TRANSACTION` and `IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION`
5. `SELECT 1` (connectivity check)
All values travel as parameters; the routine name comes from a two-entry constant, never from input.


## Revision 2026-10-22: stand-alone health check
- Settings (stand-alone template parameter -> environment variable): `DatabaseCheck` -> `SYTELINE_DB_CHECK` (`true` runs the database stages, default `false`), `SqlEncrypt` -> `SYTELINE_SQL_ENCRYPT`, `SqlVerifyHostname` -> `SYTELINE_SQL_VERIFY_HOSTNAME`, `SqlLoginTimeout` -> `SYTELINE_SQL_LOGIN_TIMEOUT`; `SYTELINE_ATTACHED` (`true` only when the function is attached to a VPC; set by the template, not a parameter).
- Rule: the stages run when not a dry run and (any route is `database` or `SYTELINE_DB_CHECK` is true) and, for `SYTELINE_DB_CHECK`, the function is attached; `DatabaseCheck=true` without a VPC attachment is refused at deployment and, if forced through the environment, reported as skipped.
- Same secret keys as the plant version; no new stored data.


## Revision 2026-10-22: automatic stand-alone check
- `SYTELINE_DB_CHECK` / `DatabaseCheck`: `auto` (default), `true`, `false` (see research.md R16). Template `AllowedValues: ["auto", "true", "false"]`; `deploy.sh --standalone` setting `DATABASE_CHECK` (default `auto`).
- Decision table (not a dry run; "routes" = the plant route settings): routes use the database -> stages run. Else mode `false` -> nothing. Mode `auto` -> stages if attached, else nothing. Mode `true` -> stages if attached, else the `database_check` skip note.
- Deployment rule: `DatabaseCheck=true` still needs the three `Attach*` settings; `auto` and `false` do not.
