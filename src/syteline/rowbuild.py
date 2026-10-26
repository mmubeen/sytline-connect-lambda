"""The rows the save writes, shaped as the PO OCR demo writes them (Intern-Demo `sytline/syteline/forms/*.py`, `formatting.py`).

What the demo shows and this module follows:
  * key values are right-justified to the ERP's column widths (CoNum 10, CoLine 4, CustNum 7, CustSeq 4, CustPo 22, Item 30, Job 10),
    and filters on those columns use the padded value too;
  * the CO header is SLCos with PascalCase names plus fixed defaults; OrderDate is the date the PO was received;
  * the CO line is written to CCT_Coitems, whose properties are lowercase column-style names (co_num, co_line, u_m, ref_num ...); the
    standard SLCoitems (PascalCase) is only READ back;
  * the job is CCTJobs, linked to its order by OrdNum / OrdLine, with Type J, Stat F and OrdType O;
  * the teardown date is a UserDefinedFields row for the line's RowPointer (TableName coitem, UDFDT1), not a property of the line;
  * dates are ISO datetimes with the Eastern offset.
The save (not the one-step upload) writes the item as the repair item code ("R* " prefix, repair_item, `repair=True`) and the description read from the item master (itemdesc, `description=`). Not done here (the demo does them, not yet asked for): UOM / due period from the customer contract, and a default teardown date of the received date plus 7 days.
"""

import re
from datetime import datetime, timedelta, timezone

from . import idos
from .errors import ErpRejected
from .validation import today

CO_NUM_WIDTH, CO_LINE_WIDTH, CUST_NUM_WIDTH, CUST_SEQ_WIDTH = 10, 4, 7, 4
CUST_PO_WIDTH, ITEM_WIDTH, JOB_NUM_WIDTH = 22, 30, 10

CO_HEADER_DEFAULTS = {"Stat": "P", "Whse": "MAIN", "TaxCode1": "R", "FrtTaxCode1": "N", "MscTaxCode1": "N", "OrigSite": "CCT"}
PACKING_TO_UF_APP = {"box": "CDBD", "crate": "YES"}
DEFAULT_WHSE = "MAIN"


def rjust(value, width):
    """Right-justify to `width` with spaces (None gives blanks)."""
    return "".rjust(width) if value is None else str(value).strip().rjust(width)


def _eastern(dt):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:  # no tz database: the US rule (2nd Sunday of March to the 1st Sunday of November), approximately
        month, day = dt.month, dt.day
        dst = 3 < month < 11 or (month == 3 and day >= 8) or (month == 11 and day < 2)
        return timezone(timedelta(hours=-4 if dst else -5))


def erp_datetime(text):
    """'YYYY-MM-DD' or 'MM/DD/YYYY' as 'YYYY-MM-DDT00:00:00-04:00' (Eastern); any other text is returned unchanged."""
    raw = str(text).strip()
    for fmt, size in (("%Y-%m-%d", 10), ("%m/%d/%Y", 10)):
        try:
            dt = datetime.strptime(raw[:size], fmt)
        except ValueError:
            continue
        local = dt.replace(tzinfo=_eastern(dt))
        offset = local.strftime("%z")
        return f"{local.strftime('%Y-%m-%dT%H:%M:%S')}{offset[:3]}:{offset[3:]}"
    return raw


def number(value):
    """A quantity as text: 3.0 -> "3", 2.5 -> "2.5"."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    return str(int(f)) if f == int(f) else str(f)


def packing_code(value):
    """Box -> CDBD, Crate -> YES; anything else is passed through unchanged (as the demo does)."""
    key = str(value).strip().lower()
    return PACKING_TO_UF_APP.get(key, str(value).strip())


def repair_item(item):
    """The SyteLine repair item code, as the demo's formatting.to_repair_item: 'PW1234' -> 'R* PW1234'; a value that already
    starts with 'R*' (any case) is left alone, so it is never prefixed twice; empty stays empty."""
    value = "" if item is None else str(item).strip()
    if not value or value.upper().startswith("R*"):
        return value
    return f"R* {value}"


def item_too_long(lines):
    """The 1-based numbers of the lines whose repair item code is longer than the ERP's item width."""
    return [i for i, ln in enumerate(lines, start=1) if len(repair_item(ln.get("item"))) > ITEM_WIDTH]


def _received(req):
    return req.get("received_date") or req.get("order_date") or today()


# -- the rows ------------------------------------------------------------------------------------------------------------
def co_header(co_num, customer, req, found=None):
    row = {"CoNum": rjust(co_num, CO_NUM_WIDTH), "CustNum": rjust(customer["cust_num"], CUST_NUM_WIDTH),
           "CustSeq": rjust(customer["cust_seq"], CUST_SEQ_WIDTH), "CustPo": rjust(req["po_number"], CUST_PO_WIDTH),
           "OrderDate": erp_datetime(_received(req))}
    for key in ("terms_code", "ship_code"):
        if found and found.get(key):
            row[idos.CO_HEADER_PROPS[key]] = found[key]
    row.update(CO_HEADER_DEFAULTS)  # spread last: the defaults win, as in the demo
    return row


def co_line(co_num, line_no, ln, job, whse=None, repair=False, description=None):
    qty = number(ln["quantity"])
    row = {"co_num": rjust(co_num, CO_NUM_WIDTH), "co_line": rjust(line_no, CO_LINE_WIDTH), "co_release": "0",
           "item": rjust(repair_item(ln["item"]) if repair else ln["item"], ITEM_WIDTH), "qty_ordered": qty, "qty_ordered_conv": qty, "u_m": ln["uom"],
           "whse": whse or DEFAULT_WHSE, "stat": "P", "ship_site": "CCT",
           "ref_type": "J", "ref_num": rjust(job, JOB_NUM_WIDTH), "ref_line_suf": "0", "ref_release": "0"}
    if description is not None:  # the save always sends one ("" when the item master has none); the one-step upload sends none
        row["description"] = description
    if ln.get("due_date"):
        row["due_date"] = erp_datetime(ln["due_date"])
    return row


def job_row(co_num, line_no, ln, job, req, whse=None, repair=False, description=None):
    job_date = erp_datetime(_received(req))
    start_end = erp_datetime(ln["due_date"]) if ln.get("due_date") else job_date
    link = idos.JOB_LINK_PROPS
    row = {"Job": rjust(job, JOB_NUM_WIDTH), "Suffix": 0, "Type": "J", "Stat": "F", "Item": rjust(repair_item(ln["item"]) if repair else ln["item"], ITEM_WIDTH),
           "Whse": whse or DEFAULT_WHSE, "QtyReleased": number(ln["quantity"]), "OrdType": "O",
           link["co_num"]: rjust(co_num, CO_NUM_WIDTH), link["co_line"]: int(line_no), "OrdRelease": 0,
           idos.JOB_EXTRA_PROPS["root_job"]: rjust(job, JOB_NUM_WIDTH), "RootSuf": 0,
           "JobDate": job_date, "JschStartDate": start_end, "JschEndDate": start_end}
    if description is not None:
        row["Description"] = description
    if ln.get("engine_sn"):
        row[idos.JOB_EXTRA_PROPS["engine_sn"]] = str(ln["engine_sn"]).strip()
    if ln.get("serial_num"):
        row[idos.JOB_EXTRA_PROPS["serial_num"]] = str(ln["serial_num"]).strip()
    if ln.get("packing"):
        row[idos.JOB_EXTRA_PROPS["packing"]] = packing_code(ln["packing"])
    return row


# -- filters (padded, as the demo's lookups) ---------------------------------------------------------------------------
def co_filter(co_num):
    return f"CoNum = {idos.quote(rjust(co_num, CO_NUM_WIDTH))}"


def job_filter(job):
    return f"Job = {idos.quote(rjust(job, JOB_NUM_WIDTH))}"


def rowpointer_filter(co_num, line_no):
    return (f"CoNum = {idos.quote(rjust(co_num, CO_NUM_WIDTH))} AND CoLine = {idos.quote(rjust(line_no, CO_LINE_WIDTH))} "
            "AND CoRelease = '0'")


# -- the teardown date: a UserDefinedFields row for the line (not a property of the line) --------------------------------
def set_teardown(soap, co_num, line_no, value):
    """Insert the UserDefinedFields row holding the line's teardown date. Raises ErpRejected when the line's RowPointer is not
    found; other ERP errors are raised as they are."""
    rows = soap.read_back(idos.LINE_IDO, ["RowPointer"], rowpointer_filter(co_num, line_no))
    pointer = rows[0].get("RowPointer") if rows else None
    if not pointer:
        raise ErpRejected("the order line's RowPointer was not found")
    soap.insert(idos.UDF_IDO, {"RowId": pointer, "TableName": "coitem", "UDFDT1": erp_datetime(value)})


def line_numbers(text):
    return re.sub(r"\D", "", str(text))
