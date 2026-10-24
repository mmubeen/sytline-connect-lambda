# Quickstart: Production-Like Mock Environment

Prerequisites: Docker (or compatible) with Compose; Python 3.9+; `pip install -r src/requirements-dev.txt -r mock/requirements.txt`. On Apple silicon enable Rosetta emulation for the SQL Server image. No AWS account or network access to the ERP is needed.

1. **Start** (first run builds the function and generates a CA): `make -C mock up` -> waits until `GET /__mock__/health` reports `ready: true` (SQL Server up to ~90 s).
2. **Full intake run** (US1): `MOCK_ENV=1 pytest tests/mock_env/test_intake_end_to_end.py` -> outcome `reserved`/`save_accepted`; second send reports `already_processed` and the ERP call record shows one write set.
3. **Failures** (US2): `MOCK_ENV=1 pytest tests/mock_env/test_faults.py` -> each catalogue fault gives its operator-facing message; nothing written to the mock ERP.
4. **Parity** (US3): `MOCK_ENV=1 pytest tests/mock_env/test_parity.py` -> settings, actions, timeout, memory, routes and permissions agree with `src/template.yaml`.
5. **Isolation** (US4): `MOCK_ENV=1 pytest tests/mock_env/test_isolation.py`; run the whole directory twice -> identical results.
6. **By hand**: `curl -X POST :4566/__mock__/scenario/needs-review`, then `curl -X POST :4566/__mock__/invoke -d @tests/fixtures/sample_event.json`.
7. **Reset / stop**: `make -C mock reset`, `make -C mock down`.

8. **Optional: real IDP (US5).** Needs your own access and a finished document:
   `aws sso login --profile idp-dev && make -C mock up MOCK_IDP=real MOCK_IDP_PROFILE=idp-dev MOCK_IDP_STACK=... MOCK_IDP_BUCKET=... MOCK_IDP_TABLE=... MOCK_IDP_DOCUMENT=159112.pdf`,
   then `MOCK_ENV=1 MOCK_IDP=real pytest tests/mock_env/test_real_idp.py`. The outcome is reported, not asserted (a real document usually stops at the customer step because the mock database is empty);
   the call records show every real call is one of the read-only IDP calls in [contracts/real-idp.md](contracts/real-idp.md). Expired credentials: `make -C mock idp-creds`.

Expected without the environment: `pytest tests/mock_env` prints "mock environment not running: start it with `make -C mock up`" and exits without pass/fail results.
