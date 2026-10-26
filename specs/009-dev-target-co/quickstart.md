# Quickstart: Development Mode

1. In the ERP, create a test order (for the customer the test document resolves to). Note its CO number, e.g. `129999`.
2. `deploy.env`: `DEV_TARGET_CO=129999`; optionally `JOB_NUMBER=TEST-001`; `SYTELINE_LIVE_WRITES=true`; remove `CO_NUMBER`. `deploy.sh` prints a warning naming the target.
3. Send a single-line document; expect `reserved`, `"development": {"target": "129999"}`, no number allocated; the save ends `succeeded` with `co_action: updated`.
4. Send the same document again with a changed quantity: it is processed (not `already_processed`); the order shows the new quantity; the ERP order count and number counters are unchanged.
5. Set `DEV_TARGET_CO` to a non-existent number: `DEV_TARGET_MISSING`, nothing written. Use a test order of another customer: `DEV_TARGET_CUSTOMER_MISMATCH`.
6. Clear `DEV_TARGET_CO`: the next document behaves as before.

Automated: `pytest tests/unit/test_devmode.py tests/unit/test_savewf.py tests/unit/test_event_intake.py`; mock env scenario `dev-target` (generate.py).
