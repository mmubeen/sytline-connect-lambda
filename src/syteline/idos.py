"""
The single place for SyteLine web-service names (IDO / collection names, method names, property
names) and for building filter expressions safely.

EVERY name here that is marked UNCONFIRMED is a guess until the read-only discovery task (T013) has
been run against the plant network; correct them here (and the payload shapes in client.py). Several
can be overridden by environment variables so a deployment can be corrected without a code change.
"""

import os

# Confirmed by docs/COJobCreationIDOs.xlsx or the instruction sheet
CO_IDO = "SLCos"
LINE_IDO = "SLCoitems"          # the standard line IDO (PascalCase, in the workbook): lines are READ back through it
COPARMS_IDO, DUE_PERIOD_PROP = "SLCoparms", "DuePeriod"  # the company-wide default due period (feature 012); UNCONFIRMED names (the demo's)
LINE_WRITE_IDO = "CCT_Coitems"  # lines are WRITTEN here (lowercase column-style names, as the PO OCR demo does); see rowbuild.py
UDF_IDO = "UserDefinedFields"   # holds a line's teardown date (UDFDT1), keyed by the line's RowPointer
JOB_IDO = "CCTJobs"
NOTE_IDO = "SpecificNotes"
OBJECT_NOTE_IDO = "ObjectNotes"
SPECIFIC_NOTE_IDO = NOTE_IDO
_DEFAULT_OBJECT_NOTE_LINK_IDO = "SLObjectNotes"  # the ERP owner's name for the note-to-record link (feature 011); UNCONFIRMED, see probe_properties
NOTE_HEADER_CO, NOTE_HEADER_COITEM, NOTE_HEADER_JOB = 31, 37, 115  # NoteHeaderToken by record type (only the order is used)
NOTE_TYPE = 0

# UNCONFIRMED: which IDO exposes each routine as a callable method, and the customer collection
_DEFAULT_CUSTOMER_IDO = "SLCustAddrs"
_DEFAULT_NEXT_CO_IDO = "SLCos"
_DEFAULT_NEXT_JOB_IDO = "SLJobs"
_DEFAULT_NOTE_METHOD_IDO = "SpecificNotes"

NEXT_CO_METHOD = "NextCoSp"
NEXT_JOB_METHOD = "NextWOSp"
CREATE_NOTE_METHOD = "CreateSpecificNoteSp"

# UNCONFIRMED property names of the customer-address collection
CUSTOMER_PROPS = {"cust_num": "CustNum", "cust_seq": "CustSeq", "name": "Name", "address1": "Addr_1",
                  "city": "City", "state": "State", "postal_code": "Zip"}

CUSTOMER_LOOKUP_CAP = 50

# Property names of the job-reservation save (feature 004), following the PO OCR demo (Intern-Demo `sytline/`); the rows themselves
# are built in rowbuild.py.
# CustPo, TermsCode and ShipCode are confirmed by docs/COJobCreationIDOs.xlsx and the demo. SLCos has NO received-date property:
# the demo writes the received date as OrderDate (rowbuild.py).
CO_HEADER_PROPS = {"cust_po": "CustPo", "terms_code": "TermsCode", "ship_code": "ShipCode"}
# Read-back names on the standard SLCoitems (the lines themselves are written to CCT_Coitems with other names)
LINE_LINK_PROPS = {"ref_type": "RefType", "ref_num": "RefNum", "ref_line_suf": "RefLineSuf", "ref_release": "RefRelease"}
LINK_NUM_PROP = LINE_LINK_PROPS["ref_num"]  # read back to confirm the line references its job
# The job's link back to its order is OrdNum / OrdLine (the workbook has no CoNum / CoLine on SLJobs or CCTJobs).
JOB_LINK_PROPS = {"co_num": "OrdNum", "co_line": "OrdLine"}
# RootJob, uf_eng_sn and uf_sn are confirmed by the workbook (CCTJobs); uf_app (the packing code) is the demo's name for `packing`.
JOB_EXTRA_PROPS = {"root_job": "RootJob", "engine_sn": "uf_eng_sn", "serial_num": "uf_sn", "packing": "uf_app"}


# Route settings (feature 005): per operation, an IDO name keeps it on the web service; `database` or empty uses the
# database (syteline.routes). The web-service defaults read loads the order collection below unless the setting names one.
NEXT_CO_SETTING = "SYTELINE_NEXT_CO_IDO"
NEXT_JOB_SETTING = "SYTELINE_NEXT_JOB_IDO"
CUSTOMER_SETTING = "SYTELINE_CUSTOMER_IDO"
DEFAULTS_SETTING = "SYTELINE_DEFAULTS_IDO"
DEFAULTS_IDO_DEFAULT = "SLCos"


def customer_ido():
    return os.environ.get("SYTELINE_CUSTOMER_IDO") or _DEFAULT_CUSTOMER_IDO


def next_co():
    return (os.environ.get("SYTELINE_NEXT_CO_IDO") or _DEFAULT_NEXT_CO_IDO, NEXT_CO_METHOD)


def next_job():
    return (os.environ.get("SYTELINE_NEXT_JOB_IDO") or _DEFAULT_NEXT_JOB_IDO, NEXT_JOB_METHOD)


def object_note_link_ido():
    return os.environ.get("SYTELINE_OBJECT_NOTE_IDO") or _DEFAULT_OBJECT_NOTE_LINK_IDO


def create_note():
    return (os.environ.get("SYTELINE_NOTE_METHOD_IDO") or _DEFAULT_NOTE_METHOD_IDO, CREATE_NOTE_METHOD)


# -- filter building: every caller-influenced value goes through these ---------------------
def quote(value):
    """A SQL-style string literal with single quotes doubled."""
    return "'" + str(value).replace("'", "''") + "'"


def filter_eq(prop, value):
    return f"{prop} = {quote(value)}"


def filter_prefix(prop, prefix):
    """`prop LIKE '<prefix>%'` with LIKE wildcards in the prefix neutralized."""
    safe = str(prefix).replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    return f"{prop} LIKE {quote(safe + '%')}"
