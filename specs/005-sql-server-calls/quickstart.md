# Quickstart: validating the SQL Server calls

Prerequisites: the repo's test setup (Python 3.12 or 3.13). Nothing here needs a real database.

## 1. Automated checks
```
python -m pytest tests/unit/test_routes.py tests/unit/test_sqlserver.py tests/unit/test_numbers.py \
    tests/unit/test_customer_match.py tests/unit/test_defaults.py tests/unit/test_no_sql.py \
    tests/unit/test_health_database.py tests/unit/test_template_database.py -q
```
Expected: all pass. They cover: route chosen from settings only and never switched after a failure (SC-009); numbers committed, rolled back on error, `UNCERTAIN_OUTCOME` when the connection drops after the batch (SC-006); 20 concurrent reservations get distinct numbers (SC-001); the demo's golden customer decisions reproduced for at least 30 cases (SC-003); no statement outside the whitelist and no row-changing statement (SC-005); encryption and certificate verification always on, warning when the host-name check is off (SC-010); no password in any output (SC-007); health reports routes and the three database stages (SC-008).

## 2. Regenerate the customer golden file (optional, needs the demo checkout)
```
python tests/tools/make_customer_golden.py "<path to>/COMPLETE_OCR/sytline/syteline"
```
Expected: `tests/fixtures/customer_match_golden.json` unchanged unless the demo's rules changed.

## 3. Template and layer
```
sam validate --lint -t scr/template.yaml
IDP_REPO=<idp checkout> make -C scr build-DepsLayer ARTIFACTS_DIR=/tmp/layer
```
Expected: valid template with the four route parameters and the 1433 egress rules; the layer builds and contains `pytds`, `OpenSSL`, `cryptography`, `rapidfuzz`.

## 4. Regression
```
python -m pytest tests -q
```
Expected: no new failures versus before this feature (the 12 failures and 2 errors in `tests/unit/test_transport.py`, `tests/contract/test_health.py` and `tests/unit/test_standalone_parity.py` already exist).

## 5. Against a real database (test configuration only, after the network and login exist)
1. Put `SQL_SERVER`, `SQL_DATABASE`, `SQL_USER`, `SQL_PASS`, `SQL_CA_PEM` in the ERP secret; deploy with the four route parameters at `database`.
2. `{"action":"health","diagnose":true}`: `routes` all `database`; `db_dns`, `db_tcp`, `db_login` pass. Fix the first failing stage (see docs/DATABASE.md).
3. With live writes **off**: reserve a PO: `LIVE_WRITES_DISABLED`, no number reserved.
4. With live writes **on** in an approved test window: reserve a 2-line PO, check in SyteLine that the numbers advanced and nothing else changed, then save it and check the header carries the customer's terms and ship-via.
Verify the `LongList` / `Infobar` types, the routine signatures and the `custaddr` / `co` column names read-only first (research.md R3).
