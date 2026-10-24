"""Regenerate the synthetic scenarios (specs/008 FR-015: synthetic data only, no production record).

    python mock/scenarios/generate.py

Every name, address, part and number below is invented. The documents mimic the SHAPE of the IDP results the function reads
(see src/syteline/idp_fields.py), not any real document.
"""

import json
import os

HERE = os.path.dirname(__file__)
ACCOUNT, REGION = "000000000000", "us-gov-east-1"
MACHINE = f"arn:aws-us-gov:states:{REGION}:{ACCOUNT}:stateMachine:mock-idp-workflow"

CUSTOMER = {"name": "MOCK AEROSPACE LTD", "addr1": "1 Example Way", "city": "Testville", "state": "CT", "zip": "06000"}


def event(document_id):
    name = f"{document_id}-00000000-0000-4000-8000-000000000001"
    arn = f"arn:aws-us-gov:states:{REGION}:{ACCOUNT}:execution:mock-idp-workflow:{name}"
    doc = json.dumps({"document": {"id": document_id, "status": "RUNNING"}})
    out = json.dumps({"document": {"id": document_id, "status": "EVALUATING"}})
    return {"version": "0", "id": "00000000-0000-4000-8000-000000000002", "detail-type": "Step Functions Execution Status Change",
            "source": "aws.states", "account": ACCOUNT, "time": "2026-10-22T23:02:29Z", "region": REGION, "resources": [arn],
            "detail": {"executionArn": arn, "stateMachineArn": MACHINE, "name": name, "status": "SUCCEEDED",
                       "startDate": 1792710108682, "stopDate": 1792710149297, "input": doc, "output": out}}


def lines(n=2):
    return [{"part_number": f"MOCK-PART-{i + 1:03d}", "quantity": i + 1, "uom": "EA"} for i in range(n)]


def good_fields():
    return {"customer_name": CUSTOMER["name"], "ship_to_address": CUSTOMER["addr1"], "ship_to_city": CUSTOMER["city"],
            "ship_to_state": CUSTOMER["state"], "ship_to_postal_code": CUSTOMER["zip"], "cust_po": "MOCK-PO-0001",
            "lines": lines()}


def idp(document_id, fields, low=(), present=True, status="COMPLETED"):
    names = {k for k in fields}
    return {"document_id": document_id, "present": present, "status": status, "class": "purchase_order", "pages": 2,
            "fields": fields,
            "confidence": {n: {"confidence": 0.55 if n in low else 0.97, "confidence_threshold": 0.8,
                               "meets_threshold": n not in low, "reason": ""} for n in sorted(names)}}


ERP = {"collections": {
    "SLCustAddrs": [{"CustNum": "C000001", "CustSeq": "0", "Name": CUSTOMER["name"], "Addr_1": CUSTOMER["addr1"],
                     "City": CUSTOMER["city"], "State": CUSTOMER["state"], "Zip": CUSTOMER["zip"]}],
    "SLCos": [{"CoNum": "MOCK-OLD-1", "CustNum": "C000001", "CustSeq": "0", "TermsCode": "N30", "ShipCode": "FOB"}]},
    "counters": {"co": 700, "job": 9000}}

SQL = ("INSERT dbo.custaddr (cust_num, cust_seq, name, [addr##1], city, state, zip, country) VALUES\n"
       f"  ('C000001', 0, '{CUSTOMER['name']}', '{CUSTOMER['addr1']}', '{CUSTOMER['city']}', '{CUSTOMER['state']}', "
       f"'{CUSTOMER['zip']}', 'USA');\n"
       "INSERT dbo.co (co_num, cust_num, cust_seq, terms_code, ship_code) VALUES ('MOCK-OLD-1', 'C000001', 0, 'N30', 'FOB');\n")

SCENARIOS = {
    "passes-review": ("A document whose every field is above its confidence threshold: job numbers are reserved and the save starts.",
                      "mock-pass-001.pdf", idp("mock-pass-001.pdf", good_fields()), {"outcome": "save_accepted"}, False),
    "already-processed": ("The same event sent twice: the second run must report the document as already processed.",
                          "mock-twice-001.pdf", idp("mock-twice-001.pdf", good_fields()),
                          {"outcome": "save_accepted", "second": "already_processed"}, True),
    "needs-review": ("A field the save needs is below its confidence threshold: nothing is written.", "mock-review-001.pdf",
                     idp("mock-review-001.pdf", good_fields(), low=("cust_po",)),
                     {"outcome": "refused", "codes": ["DOCUMENT_NEEDS_REVIEW"]}, False),
    "incomplete": ("The result lacks the PO number and the lines: refused as incomplete.", "mock-incomplete-001.pdf",
                   idp("mock-incomplete-001.pdf", {"customer_name": CUSTOMER["name"]}),
                   {"outcome": "refused", "codes": ["DOCUMENT_INCOMPLETE"]}, False),
    "not-found": ("The IDP has no record of the document.", "mock-missing-001.pdf",
                  idp("mock-missing-001.pdf", {}, present=False, status="NOT_FOUND"),
                  {"outcome": "refused", "codes": ["DOCUMENT_NOT_READY"]}, False),
}


def write(path, data):
    with open(path, "w") as f:
        f.write(data if isinstance(data, str) else json.dumps(data, indent=2, sort_keys=False) + "\n")


def main():
    for name, (desc, doc_id, idp_json, expect, twice) in SCENARIOS.items():
        d = os.path.join(HERE, name)
        os.makedirs(d, exist_ok=True)
        write(os.path.join(d, "scenario.json"), {"name": name, "description": desc, "event": event(doc_id), "expect": expect,
                                                 "send_twice": twice})
        write(os.path.join(d, "idp.json"), idp_json)
        write(os.path.join(d, "erp.json"), ERP)
        write(os.path.join(d, "sql.sql"), SQL)
        write(os.path.join(d, "secrets.json"), {})


if __name__ == "__main__":
    main()
