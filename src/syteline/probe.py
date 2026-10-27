"""Read-only discovery of ERP property names (the open items of idos.py): which property names does an IDO accept?

Each name is asked for on its own in a one-row read; the service refuses a name the IDO does not have. Only the
verdict per name is returned, never a value, and nothing is written. Used with {"action": "probe_properties", "ido": ...}.
With "list": true it instead returns the property names the IDO declares (read from the schema of a one-row read; names only).
"""

import re

from . import errors, idos, itemdesc, rowbuild
from .errors import ErpRejected, ErpUncertain, ErpUnreachable, failure

MAX_PROPERTIES = 60
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# The names the save writes or reads (idos.py), plus likely alternatives for the ones the service has refused.
DEFAULTS = {
    "CCT_Coitems": ["CoNum", "CoLine", "CoRelease", "Item", "UM", "QtyOrdered", "DueDate", "Price", "RefType", "RefNum",
                    "RefLineSuf", "RefRelease", "Teardown", "CustNum", "Whse", "CoCoNum", "OrdNum", "CoNumber", "Co_Num"],
    "SLCoitems": ["CoNum", "CoLine", "CoRelease", "Item", "UM", "QtyOrdered", "DueDate", "Price", "RefType", "RefNum",
                  "RefLineSuf", "RefRelease", "CustNum", "Whse"],
    "CCTJobs": ["Job", "OrdType", "Whse", "Item", "QtyReleased", "OrdNum", "OrdLine", "OrdRelease", "RootJob", "uf_eng_sn",
                "uf_sn", "Packing", "CoNum", "CoLine"],
    "SpecificNotes": ["SpecificNoteToken", "NoteDesc", "NoteContent", "CreateDate", "CreatedBy"],
    "SLObjectNotes": ["SpecificNoteToken", "NoteHeaderToken", "RefRowPointer", "NoteType", "NhNoteFlag"],
    "ObjectNotes": ["SpecificNoteToken", "NoteHeaderToken", "RefRowPointer", "NoteType", "NhNoteFlag"],
    "SLCos": ["CoNum", "CustNum", "CustSeq", "OrderDate", "Stat", "CustPo", "TermsCode", "ShipCode", "DateReceived", "Datefld"],
}


def _valid(event):
    ido, props = event.get("ido"), event.get("properties")
    problems = []
    if not isinstance(ido, str) or not _NAME.match(ido):
        problems.append(errors.error(errors.VALIDATION, "ido must be the name of an IDO (letters, digits, _ . -)", step="ido"))
    if props is None:
        props = DEFAULTS.get(ido)
        if props is None and isinstance(ido, str):
            problems.append(errors.error(errors.VALIDATION, "give `properties` (a list of names to try); there is no default "
                                         f"list for {ido}", step="properties"))
    elif (not isinstance(props, list) or not props or len(props) > MAX_PROPERTIES
          or not all(isinstance(p, str) and _NAME.match(p) for p in props)):
        problems.append(errors.error(errors.VALIDATION, f"properties must be a list of 1 to {MAX_PROPERTIES} property names",
                                     step="properties"))
    return ido, props, problems


def _columns(soap, event):
    """{"list": true}: the property names the IDO declares (names only), instead of testing candidate names."""
    ido = event.get("ido")
    if not isinstance(ido, str) or not _NAME.match(ido):
        return failure("probe_properties", errors.error(errors.VALIDATION, "ido must be the name of an IDO (letters, digits, _ . -)",
                                                        step="ido"))
    if not hasattr(soap, "columns"):
        return failure("probe_properties", errors.error(errors.NOT_IMPLEMENTED, "this client cannot list columns", step="list"))
    try:
        plist, names = soap.columns(ido)
    except ErpRejected as exc:
        return failure("probe_properties", errors.error(errors.ERP_REJECTED, str(exc)[:240], step="list"), ido=ido)
    except (ErpUnreachable, ErpUncertain) as exc:
        return failure("probe_properties", errors.error(errors.ERP_UNREACHABLE, str(exc)[:240], step="list"), ido=ido)
    return {"ok": True, "action": "probe_properties", "ido": ido, "property_list": plist, "columns": names}


def probe(soap, event):
    if event.get("list") is True:
        return _columns(soap, event)
    ido, props, problems = _valid(event)
    if problems:
        return failure("probe_properties", *problems)
    exists, missing = [], []
    for name in props:
        try:
            soap.load(ido, [name], "", record_cap=1)
            exists.append(name)
        except ErpRejected as exc:  # the service refused this name (or this read)
            missing.append({"property": name, "error": str(exc)[:240]})
        except (ErpUnreachable, ErpUncertain) as exc:
            return failure("probe_properties", errors.error(errors.ERP_UNREACHABLE, str(exc)[:240]),
                           ido=ido, exists=exists, missing=missing, not_tried=props[len(exists) + len(missing):])
    return {"ok": True, "action": "probe_properties", "ido": ido, "exists": exists, "missing": missing}


MAX_ROWS = 3


def _read(soap, ido, props, flt):
    try:
        return {"ido": ido, "filter": flt, "rows": [{k: str(v)[:120] for k, v in r.items()} for r in soap.load(ido, props, flt, record_cap=MAX_ROWS)]}
    except ErpRejected as exc:
        return {"ido": ido, "filter": flt, "error": str(exc)[:240]}


def probe_item(soap, event):
    """{"action": "probe_item", "item": "3810727-6"[, "co_num": "129556"]}: how the ERP holds one item, read-only.

    Tries the item master (SLItemcusts: the lookup the save uses) under the exact repair code, the 30-wide padded code and a LIKE on the
    part number, and with "co_num" also the Item and Description of that order's lines (SLCoitems, and the write table CCT_Coitems), so the stored form can be compared with
    what the save writes. Each read is reported as rows (capped, values cut at 120 characters) or the ERP's own refusal. Nothing is written.
    """
    item, co_num = event.get("item"), event.get("co_num")
    if not isinstance(item, str) or not item.strip() or len(item) > 60 or "%" in item or (co_num is not None and not re.match(r"^\d{1,10}$", str(co_num))):
        return failure("probe_item", errors.error(errors.VALIDATION, "give `item` (the part number, up to 60 characters, no %) and optionally "
                                                  "`co_num` (digits)", step="item"))
    part = item.strip()
    code = rowbuild.repair_item(part)
    props = [itemdesc.ITEM_PROP, itemdesc.DESCRIPTION_PROP]
    like = f"{itemdesc.ITEM_PROP} LIKE {idos.quote('%' + part + '%')}"
    reads = [("exact", f"{itemdesc.ITEM_PROP} = {idos.quote(code)}"), ("padded", f"{itemdesc.ITEM_PROP} = {idos.quote(rowbuild.rjust(code, rowbuild.ITEM_WIDTH))}"),
             ("like", like)]
    try:
        out = {"ok": True, "action": "probe_item", "code": code,
               "item_master": {name: _read(soap, itemdesc.ITEM_DESC_IDO, props, flt) for name, flt in reads}}
        if co_num is not None:
            out["order_lines"] = _read(soap, idos.LINE_IDO, ["CoLine", "Item", "Description"], rowbuild.co_filter(co_num))
            pad = rowbuild.rjust(co_num, rowbuild.CO_NUM_WIDTH)
            out["written_lines"] = {  # the table the save writes (lowercase names), where a line is visible even if SLCoitems hides it
                "padded": _read(soap, idos.LINE_WRITE_IDO, ["co_line", "item", "description", "ref_num"], f"co_num = {idos.quote(pad)}"),
                "like": _read(soap, idos.LINE_WRITE_IDO, ["co_line", "item", "description", "ref_num"], f"co_num LIKE {idos.quote('%' + str(co_num))}")}
    except (ErpUnreachable, ErpUncertain) as exc:
        return failure("probe_item", errors.error(errors.ERP_UNREACHABLE, str(exc)[:240]))
    return out
