# Quickstart: validating the reservation workflow

Prerequisites: the repo's Python test setup (`src/requirements-dev.txt`); Python 3.12 or 3.13. Nothing here touches a real ERP.

## 1. Automated checks (simulated ERP and table)
```
python -m pytest tests/unit/test_reserve.py tests/unit/test_savewf.py tests/unit/test_reservations_store.py \
    tests/contract/test_reservation_routes.py -q
```
Expected: all pass. They cover: distinct numbers across concurrent reserves (SC-002); duplicate `request_ref` returns the same numbers; no numbers when live writes are off or the customer is not a confident single match (SC-005); save header/line/teardown/job order and read-back (SC-003); teardown rejection becomes a warning and the job still saves; single use, other PO, other caller refused (SC-004); uncertain, rejected-after-write and read-back mismatch end as `needs_reconciliation` / `partly_saved` with nothing deleted or retried (SC-006); status shows used vs consumed-unused numbers (SC-007).

## 2. Template structure
```
sam validate --lint -t src/template.yaml
python -m pytest tests/unit/test_template_reservation.py tests/unit/test_template_network.py -q
```
Expected: three new routes on the private API, reserve and worker functions in the VPC, no new network resources, existing tests still green.

## 3. Regression
```
python -m pytest tests -q
```
Expected: no new failures versus before this feature (existing behavior unchanged, FR-017). The existing failures in `tests/unit/test_transport.py` are unrelated.

## 4. Live walkthrough (only in a test configuration with live writes deliberately on, after the read-only discovery task)
1. `POST /job-reservations` with a 2-line PO → note `reservation_id`, `co_num`, two job numbers.
2. `GET /job-reservations/{id}` → state `reserved`, both jobs `reserved`.
3. `POST /job-reservations/{id}/save` with matching PO details → 202.
4. Poll `GET` → `saved`, each line `saved`, jobs `used`. Check in SyteLine that the CO, two lines (reference = job number) and two jobs exist.
5. Repeat step 3 → 409 `RESERVATION_USED`; nothing new in SyteLine.
Property names and IDO methods are UNCONFIRMED until discovery; correct them in `src/syteline/idos.py`.
