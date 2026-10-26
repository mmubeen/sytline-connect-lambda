"""The line description: read from the ERP's item master, never taken from the document (feature 004, FR-025, research R19).

A port of the PO OCR demo's lookup_item_description: SLItemcusts exposes the item master's description as ItmDescription (the demo
uses it because the service account may not be granted SLItems). The IDO and property names below are the demo's and are UNCONFIRMED
against this ERP; a wrong name gives no description (the line is saved blank with a warning), never a failed save.
"""

from . import idos

ITEM_DESC_IDO = "SLItemcusts"
ITEM_PROP, DESCRIPTION_PROP = "Item", "ItmDescription"


def lookup(soap, code):
    """The trimmed description of the item `code` (the R*-prefixed repair item code), or None when there is none: a blank code, no
    row, a blank description, or any error reading it. Read-only; never raises."""
    code = str(code or "").strip()
    if not code:
        return None
    try:
        rows = soap.read_back(ITEM_DESC_IDO, [ITEM_PROP, DESCRIPTION_PROP], f"{ITEM_PROP} = {idos.quote(code)}", record_cap=1)
    except Exception:
        return None
    text = str((rows[0].get(DESCRIPTION_PROP) if rows else "") or "").strip()
    return text or None
