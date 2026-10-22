"""Regenerate tests/fixtures/customer_match_golden.json from the PO OCR demo's OWN customer-resolution code.

    python tests/tools/make_customer_golden.py "<path>/COMPLETE_OCR/sytline/syteline"

Needs `rapidfuzz`. The demo module opens a database only when asked to, so its `credentials`, `pyodbc` and `app_log`
imports are stubbed; only its pure functions (`_name_tokens`, `_zip_token`, `_normalize`, `_clean_address`, `_score`)
and constants are used. The pre-filter that the demo does in SQL is reproduced in Python (any of the first five name
tokens contained in the lower-cased name, or the ZIP prefix; ordered by customer then ship-to; capped at 1000).
The only side effect is writing the JSON file next to this script's fixtures directory.
"""

import importlib.util
import json
import os
import sys
import types

TABLE = [
    {"cust_num": "C0001", "cust_seq": 0, "name": "ACME AEROSPACE INC", "addr1": "1 MAIN STREET", "city": "PHOENIX", "state": "AZ", "zip": "85001", "country": "USA"},
    {"cust_num": "C0001", "cust_seq": 1, "name": "ACME AEROSPACE INC", "addr1": "500 INDUSTRIAL BLVD", "city": "TUCSON", "state": "AZ", "zip": "85701", "country": "USA"},
    {"cust_num": "C0002", "cust_seq": 0, "name": "ACME AVIATION SERVICES LLC", "addr1": "77 AIRPORT ROAD", "city": "PHOENIX", "state": "AZ", "zip": "85034", "country": "USA"},
    {"cust_num": "C0003", "cust_seq": 0, "name": "BETA TURBINE CORPORATION", "addr1": "2200 TURBINE WAY", "city": "HARTFORD", "state": "CT", "zip": "06101", "country": "USA"},
    {"cust_num": "C0003", "cust_seq": 2, "name": "BETA TURBINE CORPORATION", "addr1": "15 ENGINE DRIVE", "city": "MIDDLETOWN", "state": "CT", "zip": "06457", "country": "USA"},
    {"cust_num": "C0004", "cust_seq": 0, "name": "BETA TURBINE REPAIR CO", "addr1": "2200 TURBINE WAY", "addr2": "BLDG 4", "city": "HARTFORD", "state": "CT", "zip": "06101", "country": "USA"},
    {"cust_num": "C0005", "cust_seq": 0, "name": "GAMMA PRECISION LTD", "addr1": "9 QUEEN STREET", "city": "TORONTO", "state": "ON", "zip": "M5H 2N2", "country": "CANADA"},
    {"cust_num": "C0006", "cust_seq": 0, "name": "DELTA COMPONENTS", "addr1": "300 SOUTH AVENUE", "city": "SPOKANE", "state": "WA", "zip": "99201", "country": "USA"},
    {"cust_num": "C0007", "cust_seq": 0, "name": "DELTA COMPONENTS GROUP", "addr1": "300 SOUTH AVENUE", "city": "SPOKANE", "state": "WA", "zip": "99201", "country": "USA"},
    {"cust_num": "C0008", "cust_seq": 0, "name": "OMEGA FLIGHT SYSTEMS", "addr1": "PO BOX 1234", "city": "WICHITA", "state": "KS", "zip": "67201", "country": "USA"},
    {"cust_num": "C0009", "cust_seq": 0, "name": "MTU MAINTENANCE CANADA", "addr1": "6020 RUSSELL ROAD", "city": "RICHMOND", "state": "BC", "zip": "V6V 1Z3", "country": "CANADA"},
    {"cust_num": "C0010", "cust_seq": 0, "name": "SIGMA HYDRAULICS", "addr1": "41 LANE", "city": "DAYTON", "state": "OH", "zip": "45402", "country": "USA"},
    {"cust_num": "C0011", "cust_seq": 0, "name": "O'BRIEN & SONS MACHINING", "addr1": "8 CHURCH COURT", "city": "BOSTON", "state": "MA", "zip": "02108", "country": "USA"},
    {"cust_num": "C0012", "cust_seq": 0, "name": "KAPPA TECHNOLOGIES INTERNATIONAL", "addr1": "1000 TECH PARKWAY", "city": "AUSTIN", "state": "TX", "zip": "78701", "country": "USA"},
]

CASES = [
    ("Acme Aerospace, Inc.", "1 Main St\nPhoenix, AZ 85001"),
    ("ACME AEROSPACE INC", "500 Industrial Boulevard, Tucson AZ 85701"),
    ("Acme Aerospace", "1 Main Street Phoenix AZ"),
    ("Acme Aviation Services", "77 Airport Rd, Phoenix, AZ 85034"),
    ("ACME", "1 Main Street Phoenix AZ 85001"),
    ("Acme Aerospace Inc", "Page 1 of 2\n1 Main St\nPhoenix AZ 85001"),
    ("Beta Turbine Corp", "2200 Turbine Way, Hartford, CT 06101"),
    ("Beta Turbine Corporation", "15 Engine Dr, Middletown CT 06457"),
    ("Beta Turbine Repair", "2200 Turbine Way Bldg 4 Hartford CT 06101"),
    ("Beta Turbine", "2200 Turbine Way, Hartford CT 06101"),
    ("Gamma Precision Limited", "9 Queen St, Toronto, ON M5H 2N2"),
    ("GAMMA PRECISION", "Toronto ON"),
    ("Delta Components", "300 South Ave Spokane WA 99201"),
    ("Delta Components Group", "300 South Avenue, Spokane, WA 99201"),
    ("Delta Components Inc", "300 S Avenue Spokane WA 99201"),
    ("Omega Flight Systems", "P.O. Box 1234 Wichita KS 67201"),
    ("Omega Flight", "Wichita"),
    ("MTU Maintenance Canada", "6020 Russell Road, Richmond BC V6V 1Z3"),
    ("MTU Maintenance Canada Inc.", "Richmond, British Columbia"),
    ("Sigma Hydraulics", "41 Lane Dayton OH 45402"),
    ("Sigma Hydraulics", "999 Nowhere Street Reno NV 89501"),
    ("O'Brien and Sons Machining", "8 Church Ct, Boston MA 02108"),
    ("OBrien & Sons", "Boston MA 02108"),
    ("Kappa Technologies Intl", "1000 Tech Pkwy Austin TX 78701"),
    ("Kappa", "Austin TX 78701"),
    ("Zeta Unknown Industries", "12 Elm Street Springfield IL 62701"),
    ("The Company", "1 Main Street Phoenix AZ 85001"),
    ("Aerospace", "Phoenix AZ 85001"),
    ("Acme Aerospace Inc", "1 Main St Phoenix AZ 85034"),
    ("Delta", "Spokane WA 99201"),
    ("Beta Turbine Corporation", ""),
    ("", "2200 Turbine Way Hartford CT 06101"),
    ("  ", "   "),
    ("o'brien'; DROP TABLE co;--", "8 Church Court Boston MA 02108"),
    ("Gamma Precision", "100 Rue Sherbrooke Montreal QC H3B 1A1"),
    ("Omega Flight Systems", "1 Pine Street Seattle WA 98101"),
    ("Kappa Tech", "20 Park Avenue Boston MA 02108"),
]


def load_demo(folder):
    for name in ("credentials", "app_log", "pyodbc"):
        mod = types.ModuleType(name)
        if name == "credentials":
            mod.SQL_USER = mod.SQL_PASS = mod.SQL_SERVER = mod.SQL_DATABASE = ""
        if name == "app_log":
            import logging
            mod.get_logger = lambda n: logging.getLogger(n)
        sys.modules[name] = mod
    spec = importlib.util.spec_from_file_location("demo_customer_resolver", os.path.join(folder, "customer_resolver.py"))
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    return demo


def prefilter(demo, name, address):
    tokens = demo._name_tokens(name)
    zip5 = demo._zip_token(address)
    if not tokens and not zip5:
        return tokens, zip5, []
    hits = []
    for r in TABLE:
        if any(t in r["name"].lower() for t in tokens[:5]) or (zip5 and str(r["zip"]).startswith(zip5)):
            hits.append(r)
    hits.sort(key=lambda r: (r["cust_num"], r["cust_seq"]))
    return tokens, zip5, hits[:demo.PREFILTER_CAP]


def decide(demo, name, address):
    out = {"name": name, "address": address}
    if not name or not name.strip() or not address or not address.strip():
        out.update(tokens=[], zip5=None, status="not_found", best=None, runner_up=None)
        return out
    tokens, zip5, rows = prefilter(demo, name.strip(), address.strip())
    out.update(tokens=tokens, zip5=zip5)
    if not rows:
        out.update(status="not_found", best=None, runner_up=None)
        return out
    n = demo._normalize(name.strip())
    a = demo._normalize(demo._clean_address(address.strip()))
    scored = sorted(((r, demo._score(n, a, r)) for r in rows), key=lambda x: x[1], reverse=True)
    (best, bs), (ru, rs) = scored[0], (scored[1] if len(scored) > 1 else (None, 0.0))
    if bs >= demo.MIN_CONFIDENT_SCORE and bs - rs >= demo.AMBIGUITY_MARGIN:
        status = "match"
    elif bs >= demo.MIN_CONFIDENT_SCORE:
        status = "ambiguous"
    else:
        status = "low_confidence"
    out.update(status=status, best={"cust_num": best["cust_num"], "cust_seq": best["cust_seq"], "score": round(bs, 2)},
               runner_up=None if ru is None else {"cust_num": ru["cust_num"], "cust_seq": ru["cust_seq"],
                                                  "score": round(rs, 2)})
    return out


def main(folder):
    demo = load_demo(folder)
    data = {"min_score": demo.MIN_CONFIDENT_SCORE, "margin": demo.AMBIGUITY_MARGIN, "cap": demo.PREFILTER_CAP,
            "table": TABLE, "cases": [decide(demo, n, a) for n, a in CASES]}
    path = os.path.join(os.path.dirname(__file__), "..", "fixtures", "customer_match_golden.json")
    with open(path, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
    from collections import Counter
    print(len(data["cases"]), "cases:", dict(Counter(c["status"] for c in data["cases"])))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
