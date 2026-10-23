# Implementation Plan: SQL Server Calls for Numbers and Customer Lookup

**Branch**: `005-sql-server-calls` | **Date**: 2026-10-22 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/005-sql-server-calls/spec.md`

## Summary

Add the PO OCR demo's three database calls to the VPC deployment, each used **only for an operation that has no web-service endpoint configured**: (1) reserve the CO and job numbers by running `NextCoSp` / `NextWOSp` and committing, (2) find the customer from name and address by searching the customer address data with the demo's scoring (threshold 70, ambiguity margin 3, candidate cap 1000), (3) read the customer's default terms and ship-via codes from its most recent order. The route per operation is decided by a setting before anything is sent (an IDO name for the web service, or `database`/empty); no failure ever switches it. A new module `src/syteline/sqlserver.py` is the only code that talks to the database: a pure-Python TDS driver (`python-tds`) over a TLS connection that is encrypted and certificate-verified by default (with an explicit, warned `SYTELINE_SQL_ENCRYPT=false` option for a server that offers no verifiable certificate), with a fixed whitelist of statements (SELECT of two tables, EXEC of the two routines, COMMIT/ROLLBACK, a harmless ping) so it can never insert, update or delete a row. Existing numbering, customer-resolution and header-save code gains a route switch; the web-service code stays for operations that have an endpoint. The connectivity check reports each operation's route and the database stages. See [research.md](research.md).

**Revision 2026-10-22 (stand-alone health check):** the stand-alone Lambda also gets the database settings, used only by its connectivity check and only when attached to an existing VPC (research.md R13 to R15). No new database operation; `sqlserver.py` and the health stages are reused.

**Revision 2026-10-22 (automatic stand-alone check):** the stand-alone database check now runs automatically whenever the function is attached (`DatabaseCheck` = `auto`, the new default; `true` forces, `false` disables). See research.md R16.

**Revision 2026-10-22 (mode parity):** a new test module `tests/unit/test_mode_parity.py` keeps the settings shared by the plant and stand-alone deployments in sync across the two templates, `deploy.sh` and the example files (spec FR-015, SC-011). Tests only: no structural change (research.md R17 to R19).

## Technical Context

**Language/Version**: Python 3.13 (Lambda; existing code), SAM/CloudFormation YAML

**Primary Dependencies**: existing (zeep, lxml, requests, boto3, idp_sdk); **new**: `python-tds` (pure-Python SQL Server driver), `pyOpenSSL` + `cryptography` (TLS for the driver), `rapidfuzz` (the demo's fuzzy scoring)

**Storage**: none new (the ERP database is read through the driver; DynamoDB tables unchanged)

**Testing**: pytest with a recording fake DB-API connection (statement audit, failure injection), a golden set of customer-match decisions generated from the demo's own scoring code, route-selection tests, template tests; `cfn-lint`, `sam validate --lint`; layer build check; **no real database or deployment from this environment**

**Target Platform**: AWS GovCloud us-gov-east-1; Lambda in the plant-routed VPC; SQL Server reachable on its normal port (1433)

**Project Type**: serverless service, extension of an existing project

**Performance Goals**: 25-line reservation (1 CO + 25 job numbers) under 15 s (SC-002); customer search under 3 s for a 1000-row candidate set

**Constraints**: the database is used only for the three calls (FR-009); the only change it makes is the numbering routines' own (committed) reservation; encrypted and certificate-verified by default, an explicit warned option for an unencrypted connection and never any silent fallback (FR-010); route chosen before sending, never switched (FR-012); live writes gate applies to number reservation (FR-004); unknown outcome is `UNCERTAIN_OUTCOME`, never retried (FR-003); credentials only from the ERP secret, never logged (FR-010, FR-014)

**Scale/Scope**: tens of POs per day, up to 25 lines each

## Constitution Check

`.specify/memory/constitution.md` is still the unfilled template, so there are no ratified gates. The spec's own rules (FR-001 to FR-014) and the standing rules from features 001 to 004 were applied; the one standing rule relaxed is "no database connection" (001 FR-018, 004 FR-015), for exactly the three calls and with a stricter guard test in its place (R11). **Result: PASS (no violations).** Post-design re-check: PASS.

## Project Structure

### Documentation (this feature)

```text
specs/005-sql-server-calls/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── interfaces.md       # route settings, secret keys, health additions, error codes
└── tasks.md                # created by /speckit-tasks
```

### Source Code (repository root)

```text
src/
├── template.yaml                  # + route parameters (default `database`), SQL egress rules in create-network mode, env on Reserve/Worker/Direct, outputs
├── requirements.txt               # + python-tds, pyOpenSSL, cryptography, rapidfuzz
├── Makefile                       # + manylinux_2_28 platform tag (cryptography wheels)
├── handlers/                      # reserve.py / worker.py: map new codes (UNCERTAIN_OUTCOME for numbers, CUSTOMER_LOW_CONFIDENCE)
└── syteline/
    ├── routes.py                  # NEW: which route each operation uses (decided from settings only)
    ├── sqlserver.py               # NEW: the ONLY module that imports the driver; whitelisted statements; TLS always on
    ├── customer_match.py          # NEW: the demo's normalisation, candidate query terms, scoring and decision
    ├── numbers.py                 # NEW: NumberSource: next_co()/next_job() via the web service or the database; error classification
    ├── defaults.py                # NEW: customer defaults (terms, ship-via) via the web service or the database
    ├── resolver.py                # dispatch: web service (existing) or database (customer_match)
    ├── pipeline.py                # numbering through NumberSource; header gets defaults; uncertain number outcome parked
    ├── reserve.py                 # numbering through NumberSource (one connection per run); UNCERTAIN_OUTCOME mapping
    ├── savewf.py                  # header gets terms/ship-via; warning when defaults unavailable
    ├── health.py                  # + routes, + db_dns / db_tcp / db_login stages
    ├── config.py                  # + database settings from the secret; route helpers
    ├── idos.py                    # + SLCos terms/ship property names (UNCONFIRMED); route setting names
    └── errors.py                  # + DATABASE_NOT_CONFIGURED, CUSTOMER_LOW_CONFIDENCE
src/template-standalone.yaml     # revision: + DatabaseCheck, SqlEncrypt, SqlVerifyHostname, SqlLoginTimeout parameters and env on DirectFunction
src/syteline/health.py           # revision: database stages also when SYTELINE_DB_CHECK=true and the function is attached (SYTELINE_ATTACHED)
src/syteline/config.py           # revision: db_check(), attached()
docs/
└── DATABASE.md                    # NEW: when the database is used, settings, secret keys, TLS, least-privilege login, network
tests/
├── fakes.py                       # + FakeSqlServer (DB-API shaped, recording, scenarios)
├── fixtures/customer_match_golden.json   # NEW: decisions from the demo's scoring code
├── tools/make_customer_golden.py         # NEW: regenerates the golden file from the demo checkout
└── unit/
    ├── test_routes.py, test_sqlserver.py, test_numbers.py, test_customer_match.py, test_defaults.py,
    ├── test_no_sql.py (changed: allow only sqlserver.py, audit statements), test_health_database.py,
    └── test_template_database.py
```

**Structure Decision**: one new module owns the database (`sqlserver.py`), three small modules own the three operations and their route (`numbers.py`, `customer_match.py` + `resolver.py`, `defaults.py`), and `routes.py` is the single place the route is decided. The existing pipeline, reserve and save code call those modules instead of the web service directly, so the web-service path is unchanged when an endpoint is configured.

## Complexity Tracking

| Item | Why needed | Simpler alternative rejected because |
|---|---|---|
| A pure-Python driver plus pyOpenSSL instead of ODBC | Lambda zip deployment cannot carry the Microsoft ODBC driver and unixODBC native libraries; the repo builds layers with cross-platform wheels | ODBC would need a container image, native libraries and a licence acceptance |
| A fixed statement whitelist inside `sqlserver.py` | FR-009 and SC-005: the database may never change a row directly | Letting callers pass SQL would make the "no writes" rule unenforceable |
| Route decided from settings, no runtime detection or fallback | FR-012: an uncertain reservation must never be re-run on another route | Try-then-fall-back could reserve numbers twice |
| The demo's scoring ported with `rapidfuzz` and a golden test | SC-003: identical decisions to the demo | Re-implementing a different scorer would change which customers are accepted |
