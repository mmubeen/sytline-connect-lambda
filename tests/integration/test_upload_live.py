"""Live upload through the deployed handlers. SKIPPED unless BOTH are set:

  SYTELINE_LIVE=1          and        LIVE_APPROVAL_REF=<the ERP owner's written approval reference>

There is no test ERP. This writes ONE real customer order and its jobs, so before running, agree and
record with the ERP owner: the single test order, the customer to use (LIVE_CUSTOMER_NAME), who will
reconcile and clean up the resulting CO/lines/jobs in SyteLine, and when SYTELINE_LIVE_WRITES may be
turned on (and back off afterwards). No parallel or repeated submissions.
"""

import json
import os
import time

import pytest

from handlers import intake, status, worker

pytestmark = pytest.mark.skipif(
    os.environ.get("SYTELINE_LIVE") != "1" or not os.environ.get("LIVE_APPROVAL_REF"),
    reason="needs SYTELINE_LIVE=1 and LIVE_APPROVAL_REF (explicit ERP-owner approval)")


def test_single_approved_order():
    assert os.environ.get("SYTELINE_LIVE_WRITES", "").lower() in ("1", "true", "yes"), \
        "turn SYTELINE_LIVE_WRITES on only for the approved window"
    body = {"request_ref": f"live-{os.environ['LIVE_APPROVAL_REF']}", "confirmed": True,
            "uploaded_by": os.environ.get("LIVE_UPLOADED_BY", "live-test"),
            "customer": {"name": os.environ["LIVE_CUSTOMER_NAME"]},
            "lines": [{"item": os.environ["LIVE_ITEM"], "quantity": 1, "uom": os.environ.get("LIVE_UOM", "EA")}]}
    r = intake.handler({"body": json.dumps(body), "requestContext": {"identity": {"userArn": "live-test"}}}, None)
    upload_id = json.loads(r["body"])["upload_id"]
    worker.handler({"upload_id": upload_id}, None)
    out = json.loads(status.handler({"pathParameters": {"id": upload_id}}, None)["body"])
    print("RECONCILE THIS:", json.dumps(out, indent=2))
    assert out["state"] in ("succeeded", "needs_reconciliation", "failed")
