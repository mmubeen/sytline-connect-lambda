"""The line's due date, as the PO OCR demo computes it (feature 012): the received date plus a due period in days.

The period is the customer contract's for the line's repair item (SLItemcusts: CustNum + Item, DuePeriod), else the company-wide default
(SLCoparms.DuePeriod), else none. The IDO and property names are the demo's and are UNCONFIRMED against this ERP; a wrong name or a refused
read gives no period (the line is saved without a due date and a warning), never a failed save. Reads only; never retried.
"""

import logging
from datetime import datetime, timedelta

from . import idos, itemdesc, rowbuild

log = logging.getLogger(__name__)

CONTRACT, DEFAULT, NONE = "contract", "default", "none"


def _days(value):
    """A positive whole number of days, or None (blank, text, zero and negative are none)."""
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return days if days > 0 else None


def _read(soap, ido, props, flt):
    try:
        rows = soap.read_back(ido, props, flt, record_cap=1)
    except Exception:  # a refused or failed lookup only costs the date
        return None
    return _days(rows[0].get(idos.DUE_PERIOD_PROP)) if rows else None


def period(soap, cust_num, item_code, cache=None):
    """(days, source) for the customer and repair item: the contract's due period, else the company default, else (None, "none").

    `cache` (a dict kept for one save) holds the answers so a customer/item pair and the default are each read once."""
    cache = {} if cache is None else cache
    key = (str(cust_num or "").strip(), str(item_code or "").strip())
    if key not in cache:
        days = None
        if all(key):
            flt = f"CustNum = {idos.quote(rowbuild.rjust(key[0], rowbuild.CUST_NUM_WIDTH))} AND {itemdesc.ITEM_PROP} = {idos.quote(key[1])}"
            days = _read(soap, itemdesc.ITEM_DESC_IDO, ["CustNum", itemdesc.ITEM_PROP, idos.DUE_PERIOD_PROP], flt)
        cache[key] = (days, CONTRACT) if days else None
    if cache[key]:
        return cache[key]
    if "default" not in cache:
        cache["default"] = _read(soap, idos.COPARMS_IDO, [idos.DUE_PERIOD_PROP], "")
    days = cache["default"]
    return (days, DEFAULT) if days else (None, NONE)


def due_date(received, days):
    """The received date ('YYYY-MM-DD' or 'MM/DD/YYYY') plus `days` as the ERP datetime; None for a bad date or days <= 0."""
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        return None
    raw = str(received or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return rowbuild.erp_datetime((datetime.strptime(raw, fmt) + timedelta(days=days)).strftime("%Y-%m-%d"))
        except ValueError:
            continue
    return None
