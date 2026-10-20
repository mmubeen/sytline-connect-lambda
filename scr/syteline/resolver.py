"""
Resolve caller-supplied customer details to exactly one SyteLine customer (FR-014).

The customer number and terms come only from the SyteLine `custaddr` match, never from the caller.
Read-only SQL, parameterized.

ASSUMPTION (research.md open item 1): the instruction sheet defers to the existing PO OCR resolver,
which is not in this repo. Until its owner confirms, matching is an exact, case/whitespace-normalized
comparison on the bill-to row (cust_seq = 0): name, plus address1 / city / state / postal code when
supplied.
"""

import re

from .errors import CUSTOMER_AMBIGUOUS, CUSTOMER_NOT_FOUND, IntegrationError

# request field -> custaddr column
FIELDS = (("name", "name"), ("address1", "[addr##1]"), ("city", "city"), ("state", "state"),
          ("postal_code", "zip"))


def normalize(v):
    return re.sub(r"\s+", " ", str(v)).strip().upper()


def resolve_customer(sql, customer):
    """Return {"cust_num", "cust_seq"}; raises IntegrationError (CUSTOMER_NOT_FOUND / _AMBIGUOUS)."""
    where, params = ["cust_seq = 0"], []
    for field, column in FIELDS:
        if customer.get(field):
            where.append(f"UPPER(LTRIM(RTRIM({column}))) = %s")
            params.append(normalize(customer[field]))
    rows = sql.query(
        "SELECT cust_num, cust_seq FROM custaddr WHERE " + " AND ".join(where), tuple(params))
    matches = {(str(r["cust_num"]).strip(), r["cust_seq"]) for r in rows}
    if not matches:
        raise IntegrationError(CUSTOMER_NOT_FOUND, "no SyteLine customer matches the supplied details",
                               step="resolve_customer")
    if len(matches) > 1:
        raise IntegrationError(CUSTOMER_AMBIGUOUS,
                               f"{len(matches)} SyteLine customers match the supplied details",
                               step="resolve_customer")
    cust_num, cust_seq = next(iter(matches))
    return {"cust_num": cust_num, "cust_seq": cust_seq}
