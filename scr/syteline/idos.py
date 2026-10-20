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
LINE_IDO = "CCT_Coitems"  # named in the instruction sheet; not in the workbook
JOB_IDO = "CCTJobs"
NOTE_IDO = "SpecificNotes"
OBJECT_NOTE_IDO = "ObjectNotes"

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


def customer_ido():
    return os.environ.get("SYTELINE_CUSTOMER_IDO") or _DEFAULT_CUSTOMER_IDO


def next_co():
    return (os.environ.get("SYTELINE_NEXT_CO_IDO") or _DEFAULT_NEXT_CO_IDO, NEXT_CO_METHOD)


def next_job():
    return (os.environ.get("SYTELINE_NEXT_JOB_IDO") or _DEFAULT_NEXT_JOB_IDO, NEXT_JOB_METHOD)


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
