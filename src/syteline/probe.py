"""Read-only discovery of ERP property names (the open items of idos.py): which property names does an IDO accept?

Each name is asked for on its own in a one-row read; the service refuses a name the IDO does not have. Only the
verdict per name is returned, never a value, and nothing is written. Used with {"action": "probe_properties", "ido": ...}.
"""

import re

from . import errors
from .errors import ErpRejected, ErpUncertain, ErpUnreachable, failure

MAX_PROPERTIES = 60
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# The names the save writes or reads (idos.py), plus likely alternatives for the ones the service has refused.
DEFAULTS = {
    "CCT_Coitems": ["CoNum", "CoLine", "CoRelease", "Item", "UM", "QtyOrdered", "DueDate", "Price", "RefType", "RefNum",
                    "RefLineSuf", "RefRelease", "Teardown", "CustNum", "Whse", "CoCoNum", "OrdNum", "CoNumber", "Co_Num"],
    "CCTJobs": ["Job", "OrdType", "Whse", "Item", "QtyReleased", "OrdNum", "OrdLine", "OrdRelease", "RootJob", "uf_eng_sn",
                "uf_sn", "Packing", "CoNum", "CoLine"],
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


def probe(soap, event):
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
