# Contract: settings, health additions and error codes

No new API route. The reservation and upload routes keep their shapes; this feature adds settings, a health addition and error codes.

## Route settings (stack parameters -> function environment)
| Stack parameter | Environment variable | Default | Meaning |
|---|---|---|---|
| `NextCoIdo` | `SYTELINE_NEXT_CO_IDO` | `database` | IDO name for `NextCoSp`, or `database` |
| `NextJobIdo` | `SYTELINE_NEXT_JOB_IDO` | `database` | IDO name for `NextWOSp`, or `database` |
| `CustomerIdo` | `SYTELINE_CUSTOMER_IDO` | `database` | customer collection, or `database` |
| `DefaultsIdo` | `SYTELINE_DEFAULTS_IDO` | `database` | order collection for terms/ship-via, or `database` |
Set on `ReserveFunction`, `WorkerFunction` and `DirectFunction` (all three, so reserve, save and the health check agree). A variable that is absent keeps the earlier web-service behaviour (stand-alone version, local runs).

## Secret keys (existing ERP secret)
`SQL_SERVER`, `SQL_DATABASE`, `SQL_USER`, `SQL_PASS`, optional `SQL_PORT`, `SQL_CA_PEM` (required). See data-model.md.

## Health: `{"action":"health"}`
Added to the response (existing keys and stage order unchanged):
```json
"routes": {"next_co": "database", "next_job": "database", "customer": "webservice:SLCustAddrs", "defaults": "database"}
```
Outside the dry run, when any route is `database`, three stages are appended after `token`:
| Stage | Passes when | Failure detail (never includes credentials) |
|---|---|---|
| `db_dns` | the server name resolves | the lookup error |
| `db_tcp` | the port accepts a connection | the connect error or timeout |
| `db_login` | TLS handshake, certificate check, login and `SELECT 1` succeed | `certificate not trusted` / `host name does not match the certificate` / `login refused` / `database not configured: <missing keys>` |
Dry run (`dry_run: true`): `routes` only, no database traffic and no credentials. `ok` is true only when every stage that ran passed.

## Error codes
| Code | When |
|---|---|
| `DATABASE_NOT_CONFIGURED` | an operation's route is `database` and a required setting or `SQL_CA_PEM` is missing; refused before any connection, nothing reserved or written |
| `CUSTOMER_LOW_CONFIDENCE` | the best candidate scored below the threshold; the response lists the best candidates with scores |
| `CUSTOMER_AMBIGUOUS`, `CUSTOMER_NOT_FOUND` | as before, now also produced by the database search |
| `UNCERTAIN_OUTCOME` | a number reservation whose outcome is unknown (connection lost after the routine started); never retried |
| `ERP_UNREACHABLE` / `ERP_REJECTED` | database connect/login failure (nothing happened) / routine error or unreadable number (rolled back) |
Where a response carries these, `step` names the operation (`number_co`, `number_job_<n>`, `resolve_customer`, `customer_defaults`).

## Behaviour guarantees (testable)
- A route is decided from the settings alone; a failure on one route never starts the same operation on the other.
- The database connection is always TLS with a verified certificate chain (host-name check off only by `SYTELINE_SQL_VERIFY_HOSTNAME=false`, with a warning per connection).
- Every database statement is one of the five in data-model.md; none changes a row except the routines' own reservation.
