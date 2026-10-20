"""
Resolve caller-supplied customer details to exactly one SyteLine customer (FR-014), reading through
the web service only (FR-018).

The customer number and terms come only from the SyteLine match, never from the caller.

Lookup rule (research.md R5 step 1): load the bill-to rows of the customer-address collection whose
name starts with the first word of the normalized name (plus the postal code prefix when supplied),
capped at 50 rows (a full cap is treated as ambiguous), then compare the full normalized name and
any supplied address fields in code. Exactly one distinct customer must remain.

ASSUMPTIONS (confirm with the PO OCR resolver's owner, and the collection/property names with the
discovery task T013): the collection and property names in idos.py, bill-to is CustSeq = 0, and the
comparison rule above.
"""

import re

from . import idos
from .errors import CUSTOMER_AMBIGUOUS, CUSTOMER_NOT_FOUND, IntegrationError

P = idos.CUSTOMER_PROPS


def normalize(v):
    return re.sub(r"\s+", " ", str(v)).strip().upper()


def resolve_customer(soap, customer):
    """Return {"cust_num", "cust_seq"}; raises IntegrationError (CUSTOMER_NOT_FOUND / _AMBIGUOUS)."""
    name = normalize(customer["name"])
    where = [f"{P['cust_seq']} = 0", idos.filter_prefix(P["name"], name.split(" ")[0])]
    if customer.get("postal_code"):
        where.append(idos.filter_prefix(P["postal_code"], normalize(customer["postal_code"])[:5]))
    cap = idos.CUSTOMER_LOOKUP_CAP
    rows = soap.load(idos.customer_ido(), [P[k] for k in ("cust_num", "cust_seq", "name", "address1", "city",
                                                            "state", "postal_code")],
                     " AND ".join(where), record_cap=cap)
    if len(rows) >= cap:
        raise IntegrationError(CUSTOMER_AMBIGUOUS, f"too many candidate customers (>= {cap}); give more details",
                               step="resolve_customer")

    def same(row):
        if normalize(row.get(P["name"], "")) != name:
            return False
        for field in ("address1", "city", "state"):
            if customer.get(field) and normalize(row.get(P[field], "")) != normalize(customer[field]):
                return False
        if customer.get("postal_code") and not normalize(row.get(P["postal_code"], "")).startswith(
                normalize(customer["postal_code"])[:5]):
            return False
        return True

    matches = {(str(r[P["cust_num"]]).strip(), str(r[P["cust_seq"]]).strip()) for r in rows if same(r)}
    if not matches:
        raise IntegrationError(CUSTOMER_NOT_FOUND, "no SyteLine customer matches the supplied details",
                               step="resolve_customer")
    if len(matches) > 1:
        raise IntegrationError(CUSTOMER_AMBIGUOUS,
                               f"{len(matches)} SyteLine customers match the supplied details",
                               step="resolve_customer")
    cust_num, cust_seq = next(iter(matches))
    return {"cust_num": cust_num, "cust_seq": int(cust_seq) if cust_seq.isdigit() else cust_seq}
