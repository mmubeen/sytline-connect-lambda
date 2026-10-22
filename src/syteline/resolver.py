"""
Resolve caller-supplied customer details to exactly one SyteLine customer (FR-014).

Two routes, chosen per the `customer` setting (syteline.routes) before anything is sent: the web service (below), or,
when no endpoint is configured, the database search with the PO OCR demo's scoring (customer_match). A failure on one
route never starts the other.

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

from . import config, customer_match, idos, routes, steplog
from .errors import (CUSTOMER_AMBIGUOUS, CUSTOMER_LOW_CONFIDENCE, CUSTOMER_NOT_FOUND, IntegrationError)

P = idos.CUSTOMER_PROPS


def normalize(v):
    return re.sub(r"\s+", " ", str(v)).strip().upper()


def resolve_customer(soap, customer, cfg=None):
    """Return {"cust_num", "cust_seq"}; raises IntegrationError (CUSTOMER_NOT_FOUND / _AMBIGUOUS / _LOW_CONFIDENCE)."""
    if routes.route("customer")[0] == routes.DATABASE:
        return _resolve_in_database(customer, cfg)
    return _resolve_via_service(soap, customer)


def _candidates(result):
    out = [result.get("best"), result.get("runner_up")]
    return [c for c in out if c]


def _resolve_in_database(customer, cfg):
    from .sqlserver import SqlServer  # the one module that talks to the database

    started = steplog.now()
    try:
        with SqlServer(cfg if cfg is not None else config.load_config()) as db:
            result = customer_match.find(db, customer)
    except Exception as exc:
        steplog.step("customer_search", started, type(exc).__name__)
        raise
    status, step = result["status"], "resolve_customer"
    steplog.step("customer_search", started, status, candidates=result.get("candidates_considered", 0))
    if status == "match":
        return {"cust_num": result["cust_num"], "cust_seq": result["cust_seq"]}
    if status == "ambiguous":
        raise IntegrationError(CUSTOMER_AMBIGUOUS, "more than one SyteLine customer matches equally well; "
                               "refusing to guess", step=step, candidates=_candidates(result))
    if status == "low_confidence":
        raise IntegrationError(CUSTOMER_LOW_CONFIDENCE, "the best SyteLine customer match is not confident enough; "
                               "refusing to guess", step=step, candidates=_candidates(result))
    raise IntegrationError(CUSTOMER_NOT_FOUND, result.get("reason") or "no SyteLine customer matches the supplied "
                           "details", step=step)


def _resolve_via_service(soap, customer):
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
