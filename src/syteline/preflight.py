"""Before anything is reserved or written: does the ERP have every property the save is about to write?

A save that names a property the IDO does not have fails halfway (after the CO header is created), leaving an order with no
lines in SyteLine and burning the numbers. Each property name the save will write is therefore asked for first, alone, in a
one-row READ; a name that is refused is reported (with every other missing name) and nothing is reserved or written. A name
that passed is remembered for the life of the container. Teardown is not checked: a refused teardown date is only a warning.
Switch off with SYTELINE_CHECK_PROPERTIES=false.
"""

import os

from . import idos
from .errors import ErpRejected

_verified = set()  # (ido, property) pairs that were accepted in this container


def enabled():
    return os.environ.get("SYTELINE_CHECK_PROPERTIES", "true").strip().lower() != "false"


def needed(lines):
    """{ido: [property names the save writes]} for these PO lines (dicts with the optional keys they carry)."""
    has = lambda *keys: any(ln.get(k) for ln in lines or [] for k in keys)  # noqa: E731
    co = ["CoNum", "CustNum", "CustSeq", "OrderDate", "Stat", *idos.CO_HEADER_PROPS.values()]
    if idos.received_date_prop():
        co.append(idos.received_date_prop())
    line = ["CoNum", "CoLine", "Item", "UM", "QtyOrdered", *idos.LINE_LINK_PROPS.values()]
    line += [p for p, keys in (("DueDate", ("due_date",)), ("Price", ("price",))) if has(*keys)]
    job = ["Job", "OrdType", "Whse", "Item", "QtyReleased", *idos.JOB_LINK_PROPS.values(), idos.JOB_EXTRA_PROPS["root_job"]]
    job += [idos.JOB_EXTRA_PROPS[k] for k in ("engine_sn", "serial_num", "packing") if has(k)]
    return {idos.CO_IDO: co, idos.LINE_IDO: line, idos.JOB_IDO: job}


def check(soap, lines):
    """{ido: [{"property", "error"}]} of the names the ERP refused ({} when all are there).

    Other ERP errors (unreachable, uncertain) are raised as they are."""
    missing = {}
    for ido, props in needed(lines).items():
        for name in props:
            if (ido, name) in _verified:
                continue
            try:
                soap.load(ido, [name], "", record_cap=1)
            except ErpRejected as exc:
                missing.setdefault(ido, []).append({"property": name, "error": str(exc)[:200]})
            else:
                _verified.add((ido, name))
    return missing


def message(missing):
    parts = [f"{ido}: " + ", ".join(m["property"] for m in names) for ido, names in missing.items()]
    return ("the ERP does not have these properties, so nothing was reserved or written: " + "; ".join(parts)
            + ". Find the right names with the probe_properties action and correct src/syteline/idos.py")
