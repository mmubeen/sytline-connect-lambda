"""Request validation for POST /confirmed-orders (see data-model.md ConfirmedOrderRequest)."""

import hashlib
import json
from datetime import date, datetime

from .errors import VALIDATION, error

CUSTOMER_NUMBER_KEYS = ("customer_number", "cust_num", "custnum", "customer_num", "number")


def _is_date(v):
    try:
        datetime.strptime(str(v), "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _nonempty(v):
    return isinstance(v, str) and bool(v.strip())


def _check_customer(cust, bad):
    """Customer identification checks shared by the order upload and the job reservation requests."""
    if not isinstance(cust, dict):
        bad("customer", "customer must be an object with identifying details (name, address)")
        return
    if not _nonempty(cust.get("name")):
        bad("customer.name", "customer.name is required")
    for k in cust:
        if k.lower() in CUSTOMER_NUMBER_KEYS:
            bad(f"customer.{k}", "customer numbers are not accepted; the customer is resolved in SyteLine")
    for k in ("address1", "city", "state", "postal_code"):
        if k in cust and not isinstance(cust[k], str):
            bad(f"customer.{k}", f"customer.{k} must be a string")


def _check_line(ln, pre, bad):
    """One order line's checks, shared by the order upload and the reservation save requests."""
    if not isinstance(ln, dict):
        bad(pre, "line must be an object")
        return
    if not _nonempty(ln.get("item")):
        bad(f"{pre}.item", "item is required")
    q = _num(ln.get("quantity"))
    if q is None or q <= 0:
        bad(f"{pre}.quantity", "quantity must be a number greater than 0")
    if not _nonempty(ln.get("uom")):
        bad(f"{pre}.uom", "uom is required")
    if "due_date" in ln and not _is_date(ln["due_date"]):
        bad(f"{pre}.due_date", "due_date must be YYYY-MM-DD")
    if "price" in ln and (_num(ln["price"]) is None or _num(ln["price"]) < 0):
        bad(f"{pre}.price", "price must be a non-negative number")
    if "teardown" in ln and not isinstance(ln["teardown"], str):
        bad(f"{pre}.teardown", "teardown must be a string")


def validate_order(body):
    """Return a list of error dicts (empty when valid); all problems are reported together."""
    errs = []

    def bad(field, msg):
        errs.append(error(VALIDATION, msg, field=field))

    if not isinstance(body, dict):
        return [error(VALIDATION, "body must be a JSON object")]

    ref = body.get("request_ref")
    if not isinstance(ref, str) or not 1 <= len(ref) <= 128:
        bad("request_ref", "request_ref is required and must be 1-128 characters")
    if body.get("confirmed") is not True:
        bad("confirmed", "only operator-confirmed orders are accepted (confirmed must be true)")
    if not _nonempty(body.get("uploaded_by")):
        bad("uploaded_by", "uploaded_by is required (the confirming operator)")

    _check_customer(body.get("customer"), bad)

    if "order_date" in body and not _is_date(body["order_date"]):
        bad("order_date", "order_date must be YYYY-MM-DD")

    lines = body.get("lines")
    if not isinstance(lines, list) or not lines:
        bad("lines", "lines must contain at least one line")
        lines = []
    for i, ln in enumerate(lines):
        _check_line(ln, f"lines[{i}]", bad)
    for k in ("warehouse", "order_type"):
        if k in body and not _nonempty(body[k]):
            bad(k, f"{k} must be a non-empty string when supplied")
    return errs


def payload_hash(body):
    """SHA-256 of the canonical JSON body (detects request_ref reuse with a different payload)."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def today():
    return date.today().isoformat()


# The PO details (customer, PO number, lines) are NOT part of a reservation request: they are read from the IDP
# document the request names (syteline.idp). Sending them is refused rather than silently ignored.
DETAIL_FIELDS = ("customer", "po_number", "line_count", "lines")


def _document_request(body, bad):
    if body.get("confirmed") is not True:
        bad("confirmed", "only operator-confirmed requests are accepted (confirmed must be true)")
    if not _nonempty(body.get("uploaded_by")):
        bad("uploaded_by", "uploaded_by is required (the confirming operator)")
    if not _nonempty(body.get("document_id")):
        bad("document_id", "document_id is required (the IDP document the PO details are read from)")
    for k in DETAIL_FIELDS:
        if k in body:
            bad(k, f"{k} is not accepted: it is read from the IDP document")


def validate_reserve(body):
    """Errors for POST /job-reservations (empty when valid); all problems are reported together."""
    errs = []

    def bad(field, msg):
        errs.append(error(VALIDATION, msg, field=field))

    if not isinstance(body, dict):
        return [error(VALIDATION, "body must be a JSON object")]
    ref = body.get("request_ref")
    if not isinstance(ref, str) or not 1 <= len(ref) <= 128:
        bad("request_ref", "request_ref is required and must be 1-128 characters")
    _document_request(body, bad)
    return errs


def validate_save(body):
    """Errors for POST /job-reservations/{id}/save (empty when valid); all problems are reported together."""
    errs = []

    def bad(field, msg):
        errs.append(error(VALIDATION, msg, field=field))

    if not isinstance(body, dict):
        return [error(VALIDATION, "body must be a JSON object")]
    _document_request(body, bad)
    for k in ("order_date", "received_date"):
        if k in body and not _is_date(body[k]):
            bad(k, f"{k} must be YYYY-MM-DD")
    for k in ("warehouse", "order_type"):
        if k in body and not _nonempty(body[k]):
            bad(k, f"{k} must be a non-empty string when supplied")
    return errs


def _norm(v):
    return " ".join(str(v or "").split()).upper()


def fingerprint(customer, po_number, line_count, supplied=None):
    """SHA-256 binding a reservation to one PO: normalized customer identity, PO number and line count.

    `supplied` ({line: job number} the document carries) is part of it only when not empty, so a reservation
    without any keeps the fingerprint it always had."""
    c = customer or {}
    parts = [_norm(c.get(k)) for k in ("name", "address1", "city", "state", "postal_code")]
    parts += [_norm(po_number), str(line_count)]
    parts += [f"{line}={job}" for line, job in sorted((supplied or {}).items())]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
