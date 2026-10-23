# Contract: settings, health additions and error codes

No new API route. The reservation and upload routes keep their shapes; this feature adds settings, a health addition and error codes.

## Route settings (stack parameters -> function environment)
| Stack parameter | Environment variable | Default | Meaning |
|---|---|---|---|
| `NextCoIdo` | `SYTELINE_NEXT_CO_IDO` | `database` | IDO name for `NextCoSp`, or `database` |
| `NextJobIdo` | `SYTELINE_NEXT_JOB_IDO` | `database` | IDO name for `NextWOSp`, or `database` |
| `CustomerIdo` | `SYTELINE_CUSTOMER_IDO` | `database` | customer collection, or `database` |
| `DefaultsIdo` | `SYTELINE_DEFAULTS_IDO` | `database` | order collection for terms/ship-via, or `database` |
Set on `ReserveFunction`, `WorkerFunction` and `DirectFunction` (all three, so reserve, save and the health check agree). A variable that is absent keeps the earlier web-service behaviour (stand-alone version, local runs); an absent `DefaultsIdo` means `off` (the routes object shows `"defaults": "off"`): no defaults are read and nothing connects.

## Secret keys (existing ERP secret)
`SQL_SERVER`, `SQL_DATABASE`, `SQL_USER`, `SQL_PASS`, optional `SQL_PORT`, and `SQL_CA_PEM` (required unless the connection is explicitly unencrypted). See data-model.md.

## Encryption setting
| Stack parameter | Environment variable | Default | Meaning |
|---|---|---|---|
| `SqlEncrypt` | `SYTELINE_SQL_ENCRYPT` | `true` | `true`: encrypted, certificate verified, `SQL_CA_PEM` required. `false` (exact value): unencrypted connection allowed, no CA needed, warning on every connection. Set on `ReserveFunction`, `WorkerFunction` and `DirectFunction` |

## Health: `{"action":"health"}`
Added to the response (existing keys and stage order unchanged):
```json
"routes": {"next_co": "database", "next_job": "database", "customer": "webservice:SLCustAddrs", "defaults": "off"}
```
Outside the dry run, when any route is `database`, three stages are appended after `token`:
| Stage | Passes when | Failure detail (never includes credentials) |
|---|---|---|
| `db_dns` | the server name resolves | the lookup error |
| `db_tcp` | the port accepts a connection | the connect error or timeout |
| `db_login` | TLS handshake and certificate check (when encrypted), login and `SELECT 1` succeed; the detail says `encrypted` or `UNENCRYPTED` | `certificate not trusted` / `host name does not match the certificate` / `login refused` / `database not configured: <missing keys>` |
When the connection is unencrypted the response also carries `"warnings": ["database connection is UNENCRYPTED: the login and password cross the network unprotected"]`. Dry run (`dry_run: true`): `routes` only, no database traffic and no credentials. `ok` is true only when every stage that ran passed.

## Error codes
| Code | When |
|---|---|
| `DATABASE_NOT_CONFIGURED` | an operation's route is `database` and a required setting is missing (including `SQL_CA_PEM` while encrypted); refused before any connection, nothing reserved or written |
| `CUSTOMER_LOW_CONFIDENCE` | the best candidate scored below the threshold; the response lists the best candidates with scores |
| `CUSTOMER_AMBIGUOUS`, `CUSTOMER_NOT_FOUND` | as before, now also produced by the database search |
| `UNCERTAIN_OUTCOME` | a number reservation whose outcome is unknown (connection lost after the routine started); never retried |
| `ERP_UNREACHABLE` / `ERP_REJECTED` | database connect/login failure (nothing happened) / routine error or unreadable number (rolled back) |
Where a response carries these, `step` names the operation (`number_co`, `number_job_<n>`, `resolve_customer`, `customer_defaults`).

## Behaviour guarantees (testable)
- A route is decided from the settings alone; a failure on one route never starts the same operation on the other.
- By default the database connection is TLS with a verified certificate chain (host-name check off only by `SYTELINE_SQL_VERIFY_HOSTNAME=false`, with a warning per connection). The only way to an unencrypted connection is `SYTELINE_SQL_ENCRYPT=false`, which warns on every connection and is reported by the connectivity check; no failure, missing CA or handshake error ever leads to an unencrypted connection.
- Every database statement is one of the five in data-model.md; none changes a row except the routines' own reservation.


## Revision 2026-10-22: stand-alone health check
| Stand-alone parameter | Environment variable | Default | Meaning |
|---|---|---|---|
| `DatabaseCheck` | `SYTELINE_DB_CHECK` | `false` | `true`: the health check also runs `db_dns`, `db_tcp`, `db_login`. Requires the three `Attach*` settings (refused otherwise) |
| `SqlEncrypt` | `SYTELINE_SQL_ENCRYPT` | `true` | as in the plant version (`false` = unencrypted, warned, no CA needed) |
| `SqlVerifyHostname` | `SYTELINE_SQL_VERIFY_HOSTNAME` | `true` | as in the plant version |
| `SqlLoginTimeout` | `SYTELINE_SQL_LOGIN_TIMEOUT` | `5` | seconds for the connection and login |
| (template) | `SYTELINE_ATTACHED` | `true` when attached, else `false` | lets the function know it can reach a private network |

Health response, stand-alone, `DatabaseCheck=true`: attached -> the stages and the unencrypted warning exactly as in the plant version; not attached (only reachable by forcing the environment) -> no database stage and `"database_check": "skipped: the function is not attached to a VPC"`. With `DatabaseCheck=false` (default) the response is unchanged from today.
Secret keys, error and stage messages: as in the sections above. The stand-alone actions `add_note` and `get_notes` never use the database.


## Revision 2026-10-22: automatic stand-alone check
`DatabaseCheck` / `SYTELINE_DB_CHECK` is now `auto` | `true` | `false`, default `auto` (the table above showed `true`/`false`, default `false`; this supersedes it):
- `auto`: the stand-alone health check runs `db_dns`, `db_tcp`, `db_login` (and the unencrypted warning) whenever the function is attached to a VPC; a function that is not attached shows exactly the earlier result.
- `true`: as `auto`, and refused at deployment without the three `Attach*` settings.
- `false`: never, whatever the attachment.
A missing database key makes `db_login` fail naming the keys (`database not configured: ...`) and the overall `ok` false. The plant deployment never sets this variable and is unchanged.


## Revision 2026-10-22: parity contract (test contract, no runtime interface)
`tests/unit/test_mode_parity.py` exposes pure functions used by the tests and the self-test: `shared_parameters(plant, standalone)`, `template_violations(plant, standalone)`, `wiring_violations(plant, standalone)`, `script_violations(script_text, plant, standalone)`, `example_violations(example_texts, plant, standalone)`; each returns a list of violation strings (empty = in sync). Adding a setting to both modes must require updating all four places or the suite fails; adding it to one mode only never fails the parity tests.

## Settings file (revision: one file for both modes)

`src/deploy.env` (copied from `src/deploy.env.example`, git-ignored) is read by `./deploy.sh` and `./deploy.sh --standalone`.

| Block | Variables | Read by |
|---|---|---|
| Shared | `SYTELINE_WSDL`, `SYTELINE_USER`, `SYTELINE_SECRET_ARN`, `SYTELINE_CONFIG`, `AWS_PROFILE_NAME`, `LIVE_WRITES`, `LIVE_APPROVAL_REF`, `USE_FIPS_ENDPOINTS`, `SQL_SERVER`, `SQL_DATABASE`, `SQL_USER`, `SQL_PASS`, `SQL_PORT`, `SQL_CA_PEM_FILE`, `SQL_ENCRYPT`, `SQL_VERIFY_HOSTNAME`, `SQL_LOGIN_TIMEOUT` | both modes |
| `PLANT ONLY` | `WORKER_*`, `CREATE_VPC_ENDPOINTS`, `HOST_NETWORK_CIDR`, `PLANT_CIDR_*`, `DYNAMODB_PREFIX_LIST_ID`, `API_VPCE_ID`, `CREATE_API_ENDPOINT`, `IDP_*`, `*_IDO`, `SITE_ID`, `SQL_TIMEOUT`, `MATCH_*`, `SYTELINE_DEFAULT_*` | plant mode; ignored by `--standalone` |
| `STAND-ALONE ONLY` | `ATTACH_VPC_ID`, `ATTACH_SUBNET_IDS`, `ATTACH_SG_IDS`, `ALLOW_PLAIN_HTTP`, `VERIFY_TLS`, `DATABASE_CHECK` | `--standalone`; ignored by plant mode |

`./deploy.sh --standalone` with a `deploy.standalone.env` present exits before building: "deploy.standalone.env is no longer read: move its settings into deploy.env (stand-alone block) and delete it". No other behaviour changes.

## Diagnostics (`{"diagnose": true}`), revision: database probe

Added keys of the `diagnostics` section, present only when the health check would run the database stages (spec FR-019):

```json
"database": {"host": "sql.plant.example", "port": 1433, "ok": true, "attempts": [...], "addresses": ["10.1.2.3"],
             "connections": [{"address": "10.1.2.3", "ok": true, "ms": 4}], "connected": true},
"dns_raw": {"erp": [...], "aws": [...], "database": [...]},
"findings": ["... existing entries ...", "The database host and port are reachable: ..."]
```

or, when the rule says probe but it cannot be done: `"database_skipped": "<reason>"` (dry run / secret not readable / no `SQL_SERVER` in the secret). When the rule says no probe, none of these keys is added. No login, TLS handshake, credential, query or data is sent; `stages`, `ok` and `warnings` are unchanged by the probe.

