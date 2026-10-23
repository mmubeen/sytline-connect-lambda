# The SyteLine database calls

ERP records (customer order header, lines, jobs, teardown dates, notes) are saved **through the SyteLine web service**. The SyteLine database (SQL Server) is used only for three calls, as in the PO OCR demo, and **only for an operation that has no web-service endpoint configured**:

| Operation | What the database does | Setting |
|---|---|---|
| Reserve the CO number, reserve job numbers | runs SyteLine's own `NextCoSp` / `NextWOSp` and commits, so the number belongs to the caller | `NextCoIdo`, `NextJobIdo` |
| Find the customer from the PO's name and address | searches `custaddr` and scores the candidates (the demo's rules); only one confident match is accepted | `CustomerIdo` |
| Read the customer's default terms and ship-via | reads them from the customer's most recent order (`co`), for the new order's header | `DefaultsIdo` |

The database never inserts, updates or deletes a row directly; the only change is the numbering routines' own reservation of a number. Design: [`specs/005-sql-server-calls/`](../specs/005-sql-server-calls/).

## Which route an operation uses

Each setting is either an **endpoint (IDO) name**, which keeps that operation on the web service, or `database` (or empty), which uses the database. It is decided from the setting alone, before anything is sent: nothing is probed, and **no failure, timeout or unknown outcome ever starts the same operation on the other route** (so one PO's numbers never come from two sources). The connectivity check shows the route of every operation (`routes`).

- The stack parameters default to `database` (`deploy.env`: `NEXT_CO_IDO`, `NEXT_JOB_IDO`, `CUSTOMER_IDO`, `DEFAULTS_IDO`).
- **Updating an existing VPC stack therefore switches its numbering, customer search and defaults to the database.** Without database settings in the secret they fail clearly (`DATABASE_NOT_CONFIGURED`) before anything is reserved or written. Setting a parameter to the old endpoint name keeps that operation on the web service.
- A function whose route variable is absent (the stand-alone version, local runs) behaves as before: the web service for numbers and the customer search, and no defaults read.

## Settings (in the existing ERP credentials secret)

| Key | Meaning |
|---|---|
| `SQL_SERVER` | host name; must match the server certificate (unless the host-name check is switched off, below) |
| `SQL_DATABASE` | the SyteLine database |
| `SQL_USER`, `SQL_PASS` | the login (never logged or returned) |
| `SQL_PORT` | optional, default 1433 |
| `SQL_CA_PEM` | PEM text of the certificate(s) to trust (the server's certificate authority, or, for a self-signed test server, its own certificate). **Required while the connection is encrypted** (the default); not needed with `SqlEncrypt=false` |

### Putting the values into the secret (`deploy.sh`)

You can fill them in `deploy.env` (the one settings file for both stacks) and let `deploy.sh` do it: `SQL_SERVER`, `SQL_DATABASE`, `SQL_USER`, `SQL_PASS`, `SQL_PORT` and `SQL_CA_PEM_FILE` (a path to a PEM file; its text is stored as `SQL_CA_PEM`). Before building, the script reads the secret named by `SYTELINE_SECRET_ARN`, **merges** the values you gave into it (its other keys are kept; a value left empty keeps what the secret already holds) and writes a new version. Rules:

- It asks first (`./deploy.sh --yes` skips the question); with values set and no terminal it refuses rather than guess.
- The values travel only through the environment and a private temporary file that is removed afterwards: they are never stack parameters, never on a command line and never printed (only the key names are).
- If the secret already holds exactly these values nothing is written. A problem reading or writing the secret, a secret that is not a JSON object, a bad `SQL_PORT` or a missing PEM file stops the run before anything is built or deployed.
- `--build` and `--validate` never touch the secret. The caller needs `secretsmanager:GetSecretValue` and `PutSecretValue` on it.
- `deploy.env` is git-ignored; once the secret has the values you may delete them from the file.

Every function that can read the secret can read these. Use a login with only the rights the calls need: **execute** on `NextCoSp` and `NextWOSp` and **select** on `custaddr` and `co`.

Tunables (environment): `SYTELINE_SITE_ID` (default 10), `SYTELINE_SQL_LOGIN_TIMEOUT` (5 s), `SYTELINE_SQL_TIMEOUT` (10 s), `SYTELINE_MATCH_MIN_SCORE` (70), `SYTELINE_MATCH_MARGIN` (3), `SYTELINE_MATCH_CAP` (1000).

Each is a stack parameter (`SiteId`, `SqlLoginTimeout`, `SqlTimeout`, `SqlVerifyHostname`, `MatchMinScore`, `MatchMargin`, `MatchCap`) set from `deploy.env` (`SITE_ID`, `SQL_LOGIN_TIMEOUT`, `SQL_TIMEOUT`, `SQL_VERIFY_HOSTNAME`, `MATCH_MIN_SCORE`, `MATCH_MARGIN`, `MATCH_CAP`). `deploy.sh` always passes them, with the defaults above, so a value set by an earlier deployment is cleared; it refuses values that are not numbers or `true`/`false`.

## Encryption and certificates

**By default** the connection is encrypted and its certificate chain is verified, and `SQL_CA_PEM` is required. Without it nothing connects (`DATABASE_NOT_CONFIGURED`): a missing CA, a failed handshake or any other error **never** leads to an unencrypted connection. The host name is checked against the certificate; `SYTELINE_SQL_VERIFY_HOSTNAME=false` turns **only that check** off (for example when the server is reached by IP address) and logs a warning on every connection. The driver speaks TLS 1.2 (SQL Server 2016 to 2022; not strict-encryption mode).

**Explicit unencrypted option.** If the plant server offers no certificate the functions can verify (the demo connects with `Encrypt=no`), set the stack parameter `SqlEncrypt` to `false` (`SQL_ENCRYPT=false` in `deploy.env`; environment variable `SYTELINE_SQL_ENCRYPT`, only the exact value `false` counts). Then:

- no CA certificate is needed, and the connection is **not encrypted**: the database login and password cross the plant network unprotected;
- every connection logs `database connection is UNENCRYPTED ...`;
- the connectivity check says so in the `db_login` detail and adds the same text to `warnings`;
- a SQL Server set to **Force Encryption** refuses an unencrypted client: either supply a CA and keep the default, or turn Force Encryption off on that server.

The other settings (`SQL_SERVER`, `SQL_DATABASE`, `SQL_USER`, `SQL_PASS`) stay required in both modes.

## Network

The functions run in the plant-routed network. They need a route to the database on **TCP 1433** and a security group that allows it. When the stack creates the host network it adds the 1433 egress rules to the plant ranges (`PlantCidr1..3`); with an existing network your security group must allow it. The stack creates no other network rule.

## Failures

| Situation | Result |
|---|---|
| A required setting is missing (including `SQL_CA_PEM` while encrypted) | `DATABASE_NOT_CONFIGURED` (names the keys only), nothing sent; never an unencrypted fallback |
| Cannot connect or log in | `ERP_UNREACHABLE`; nothing happened |
| A numbering routine reports an error or no number | rolled back, `ERP_REJECTED` naming the routine |
| The connection is lost after a routine started, or during the commit | `UNCERTAIN_OUTCOME`: a number may be consumed; never retried; the number is treated as consumed |
| Live writes are off | no number is requested (`LIVE_WRITES_DISABLED`) |
| Customer match is ambiguous, below the threshold or not found | `CUSTOMER_AMBIGUOUS` / `CUSTOMER_LOW_CONFIDENCE` / `CUSTOMER_NOT_FOUND`, with the best candidates and scores; nothing is reserved or written |
| The customer defaults cannot be read, or the customer has no orders | the order is saved without terms and ship-via and a warning says why (shown by the reservation status) |

## Diagnosing the database connection

`{"action": "health", "diagnose": true}` adds a database probe to the `diagnostics` section **only when the health check itself tests the database** (a plant operation uses the database route, or the stand-alone function is attached and `DatabaseCheck` is `auto` or `true`). It uses the server name and port the health check read from the credentials secret and does a name lookup (every attempt), a direct question to the function's nameserver, and a TCP connection test to each address (`diagnostics.database`, `diagnostics.dns_raw.database`), then adds database lines to `diagnostics.findings`:

| Finding | Meaning |
|---|---|
| the nameserver says the database name does not exist (NXDOMAIN) | plant DNS is not reachable from the function's network, or `SQL_SERVER` should be an address |
| the host resolves but no connection to the port succeeded | missing route to the plant, a security group or firewall blocking the port, or SQL Server not listening |
| the database host and port are reachable | the cause is the login, the certificate or the encryption setting: read the `db_login` stage's error |

It never logs in and sends no credential, query or data, and `stages`, `ok` and `warnings` are the same as without `diagnose`. When it cannot run it says so in `diagnostics.database_skipped`: a dry run does not read the secret, the secret could not be read, the secret holds no `SQL_SERVER`, or `SQL_PORT` is not a number. When the health check would not test the database, the probe and the key are absent.

## Stand-alone version

The stand-alone Lambda has no actions that reserve numbers, search customers or save orders, so it never uses the database for them. When it is **attached to an existing VPC** it can run the same connectivity stages (`db_dns`, `db_tcp`, `db_login`, with the encryption detail and the unencrypted warning) in its health check:

| `deploy.env` (stand-alone block) | Stack parameter | Default | Meaning |
|---|---|---|---|
| `DATABASE_CHECK` | `DatabaseCheck` | `auto` | `auto`: the database stages run whenever the function is attached to a VPC (not attached: unchanged). `true`: always (refused unless all three `ATTACH_*` settings are given). `false`: never |
| `SQL_ENCRYPT` | `SqlEncrypt` | `true` | as above (`false` = unencrypted, warned, no CA needed) |
| `SQL_VERIFY_HOSTNAME` | `SqlVerifyHostname` | `true` | as above |
| `SQL_LOGIN_TIMEOUT` | `SqlLoginTimeout` | `5` | seconds |

The database keys go in the secret, and the security group must allow TCP 1433. With `auto` an attached function shows the stages on every health call; a database key missing from the secret appears as a failing `db_login` stage naming the key, and the overall `ok` is false (set `DATABASE_CHECK=false` to keep the earlier result). An unattached function is unchanged. Only a forced `true` that reaches an unattached function (the template and script refuse it) reports `database_check: skipped: the function is not attached to a VPC`. See [STANDALONE.md](STANDALONE.md).

## Checking it (before the first PO)

`{"action":"health"}` reports `routes` and, when any operation uses the database, three separate stages: `db_dns` (the name resolves), `db_tcp` (the port accepts a connection) and `db_login` (TLS handshake and certificate when encrypted, login, then `SELECT 1`; the detail says `encrypted` or `UNENCRYPTED`). Failures say which: `certificate not trusted`, `host name does not match the certificate`, `login refused`, `database not configured: <keys>`. Nothing is changed and no credential is shown. The credential-free dry run reports `routes` only.

## Unconfirmed: verify read-only first

These come from the demo and have only been tested against fakes: the `LongList` and `Infobar` types and the `NextCoSp` / `NextWOSp` signatures, the `custaddr` columns (`cust_num`, `cust_seq`, `name`, `addr##1..4`, `city`, `state`, `zip`, `country`), the `co` columns (`co_num`, `cust_num`, `cust_seq`, `terms_code`, `ship_code`), and the `SLCos` property names for terms and ship-via (`TermsCode`, `ShipCode` in `src/syteline/idos.py`). Do not enable live writes until they have been checked against the plant database.

Regenerating the customer-match golden file from the demo's own code: `python tests/tools/make_customer_golden.py "<path to>/COMPLETE_OCR/sytline/syteline"` (needs `rapidfuzz`).
