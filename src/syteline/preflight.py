"""Before anything is reserved or written: does the ERP have every property the save is about to write?

A save that names a property the IDO does not have fails halfway (after the CO header is created), leaving an order with no
lines in SyteLine and burning the numbers. Each property name the save will write is therefore asked for first, alone, in a
one-row READ; a name that is refused is reported (with every other missing name) and nothing is reserved or written. A name
that passed is remembered for the life of the container. The names come from the rows rowbuild builds, so the check cannot drift
from the save. Switch off with SYTELINE_CHECK_PROPERTIES=false.
"""

import os

from . import idos, rowbuild
from .errors import ErpRejected

_verified = set()  # (ido, property) pairs that were accepted in this container


def enabled():
    return os.environ.get("SYTELINE_CHECK_PROPERTIES", "true").strip().lower() != "false"


def needed(lines):
    """{ido: [property names the save writes or reads back]} for these PO lines: the names of the rows rowbuild builds (the union
    over the lines, so optional ones appear only when some line carries them), so the check cannot drift from the save."""
    lines = lines or [{}]
    sample = {"item": "I", "quantity": 1, "uom": "EA"}
    req = {"po_number": "P", "received_date": "2026-01-01"}
    cust = {"cust_num": "1", "cust_seq": 0}
    found = {"terms_code": "T", "ship_code": "S"}
    line_props, job_props = [], []
    for ln in lines:
        full = {**sample, **{k: v for k, v in ln.items() if k in ("due_date", "engine_sn", "serial_num", "packing") and v}}
        for src, dst in ((rowbuild.co_line("1", 1, full, "1"), line_props), (rowbuild.job_row("1", 1, full, "1", req), job_props)):
            dst += [k for k in src if k not in dst]
    udf = any(ln.get("teardown") for ln in lines)
    out = {idos.CO_IDO: list(rowbuild.co_header("1", cust, req, found)) + [],
           idos.LINE_WRITE_IDO: line_props,
           idos.LINE_IDO: ["CoNum", "CoLine", "Item", "QtyOrdered", *idos.LINE_LINK_PROPS.values()] + (["RowPointer"] if udf else []),
           idos.JOB_IDO: job_props}
    if udf:
        out[idos.UDF_IDO] = ["RowId", "TableName", "UDFDT1"]
    return out


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
