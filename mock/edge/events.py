"""The Step Functions "execution succeeded" event the IDP state machine sends, for a given document id (specs/008).

Used by the synthetic scenarios and by the real-IDP mode, which names a real document. The shape follows tests/fixtures/sample_event.json;
the account, region and state machine are placeholders, nothing here is a real identifier.
"""

import json
import re

ACCOUNT, REGION = "000000000000", "us-gov-east-1"
MACHINE = f"arn:aws-us-gov:states:{REGION}:{ACCOUNT}:stateMachine:mock-idp-workflow"
_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")  # the rule the function applies (syteline.eventdoc)


def build_event(document_id):
    if not _DOCUMENT_ID.match(document_id or "") or ".." in document_id:
        raise ValueError("the document id must be 1 to 256 letters, digits, . _ - / (and no '..')")
    name = f"{document_id}-00000000-0000-4000-8000-000000000001"
    arn = f"arn:aws-us-gov:states:{REGION}:{ACCOUNT}:execution:mock-idp-workflow:{name}"
    doc = json.dumps({"document": {"id": document_id, "status": "RUNNING"}})
    out = json.dumps({"document": {"id": document_id, "status": "EVALUATING"}})
    return {"version": "0", "id": "00000000-0000-4000-8000-000000000002", "detail-type": "Step Functions Execution Status Change",
            "source": "aws.states", "account": ACCOUNT, "time": "2026-10-22T23:02:29Z", "region": REGION, "resources": [arn],
            "detail": {"executionArn": arn, "stateMachineArn": MACHINE, "name": name, "status": "SUCCEEDED",
                       "startDate": 1792710108682, "stopDate": 1792710149297, "input": doc, "output": out}}
