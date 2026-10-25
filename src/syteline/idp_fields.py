"""Where the PO details sit in an IDP document's inference result, and how to read them.

ALL NAMES HERE ARE UNCONFIRMED until the owner of the IDP configuration supplies the document class and field
names (specs/004-job-number-workflow/research.md R14). The defaults follow the PO OCR demo's payload
(`customer_name`, `ship_to_address`, `cust_po`, `date_received`, `lines[].part_number` ...). Correct them here or,
without a code change, set IDP_FIELD_MAP (a JSON object merged over these defaults) and IDP_DOCUMENT_CLASS.

Paths are dotted names into the result's `fields` (a path through nested objects). `lines` names the list of
line items; each line's own field names are in LINE.
"""

import json
import os
import re
from datetime import datetime

DEFAULT_MAP = {
    "customer.name": "customer_name",       # required
    "customer.address1": "ship_to_address",
    "customer.city": "ship_to_city",
    "customer.state": "ship_to_state",
    "customer.postal_code": "ship_to_postal_code",
    "po_number": "cust_po",                 # required
    "received_date": "date_received",
    "order_date": "order_date",
    "lines": "lines",                       # required: the list of line items
    "line.item": "part_number",             # required per line
    "line.quantity": "quantity",            # required per line
    "line.uom": "uom",                      # required per line
    "line.due_date": "due_date",
    "line.price": "price",
    "line.teardown": "teardown",
    "line.engine_sn": "engine_sn",
    "line.serial_num": "serial_num",
    "line.packing": "packing",
    "line.job": "job_number",               # optional: a job number the document already carries for the line (FR-006e)
}
REQUIRED = ("customer.name", "po_number", "lines")
REQUIRED_LINE = ("line.item", "line.quantity", "line.uom")
MAX_LINES = 25

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d")


def field_map():
    """The default mapping with any IDP_FIELD_MAP overrides applied."""
    m = dict(DEFAULT_MAP)
    raw = os.environ.get("IDP_FIELD_MAP", "")
    if raw:
        m.update({k: str(v) for k, v in json.loads(raw).items() if k in DEFAULT_MAP})
    return m


def document_class():
    """The expected IDP document class, or None to accept any (UNCONFIRMED)."""
    return os.environ.get("IDP_DOCUMENT_CLASS") or None


def _get(data, path):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _text(v):
    if v is None or isinstance(v, (dict, list)):
        return None
    t = " ".join(str(v).split())
    return t or None


def _number(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(re.sub(r"[,\s$]", "", str(v)))
    except ValueError:
        return None


def _date(v):
    t = _text(v)
    if t is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    return False  # present but not a date


_CONVERT = {"text": _text, "number": _number, "date": _date}
_LINE_KINDS = {"item": "text", "quantity": "number", "uom": "text", "due_date": "date", "price": "number",
               "teardown": "text", "engine_sn": "text", "serial_num": "text", "packing": "text", "job": "text"}


def _present(raw):
    return raw is not None and (isinstance(raw, (int, float)) or _text(raw) is not None)


def _convert(name, raw):
    """The cleaned value, or None when it is present but unusable."""
    out = _CONVERT[_LINE_KINDS.get(name, "text")](raw)
    if out is None or out is False:
        return None
    if (name == "quantity" and out <= 0) or (name == "price" and out < 0):
        return None
    return out.upper() if name == "uom" else out


def map_fields(fields, mapping=None):
    """Map an IDP result's `fields` to PO details.

    Returns (details, missing, used): `missing` lists EVERY required field that is absent or unreadable (as
    "<name>" or "lines[i].<name>"), and optional fields that are present but unreadable; `used` lists the concrete
    result paths that were read (for the confidence check). `details` is
    {customer, po_number, received_date?, order_date?, lines}.
    """
    m = mapping or field_map()
    fields = fields if isinstance(fields, dict) else {}
    missing, used = [], []

    def header(key, kind="text"):
        raw = _get(fields, m[key])
        if not _present(raw):
            return None, False
        used.append(m[key])
        val = _CONVERT[kind](raw)
        return (val, False) if val not in (None, False) else (None, True)  # (value, present-but-unusable)

    customer = {}
    for key, name in (("customer.name", "name"), ("customer.address1", "address1"), ("customer.city", "city"),
                      ("customer.state", "state"), ("customer.postal_code", "postal_code")):
        val, unusable = header(key)
        if val:
            customer[name] = val
        elif key in REQUIRED or unusable:
            missing.append(key)
    details = {"customer": customer}
    po, unusable = header("po_number")
    if po:
        details["po_number"] = po
    else:
        missing.append("po_number")
    for key in ("received_date", "order_date"):
        val, unusable = header(key, "date")
        if val:
            details[key] = val
        elif unusable:
            missing.append(key)

    raw_lines = _get(fields, m["lines"])
    lines = []
    if not isinstance(raw_lines, list) or not raw_lines:
        missing.append("lines")
    else:
        used.append(m["lines"])
        for i, raw in enumerate(raw_lines):
            if not isinstance(raw, dict):
                missing.append(f"lines[{i}]")
                continue
            line = {}
            for name in _LINE_KINDS:
                key = f"line.{name}"
                val = raw.get(m[key])
                if not _present(val):
                    if key in REQUIRED_LINE:
                        missing.append(f"lines[{i}].{name}")
                    continue
                used.append(f"{m['lines']}[{i}].{m[key]}")
                out = _convert(name, val)
                if out is None:
                    missing.append(f"lines[{i}].{name}")
                else:
                    line[name] = out
            lines.append(line)
    details["lines"] = lines
    return details, missing, used
