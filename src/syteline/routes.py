"""Which route each operation uses: the web service (an endpoint is configured) or the database (none is).

Decided from the settings alone (specs/005-sql-server-calls, FR-012). Nothing here probes the service, retries or
switches after a failure: a call that has started stays on its route.

Per operation there is one setting (idos.NEXT_CO_SETTING ...):
  absent                          -> the earlier web-service default (so stacks and tests that never set it are unchanged);
                                     `defaults` has no earlier behaviour, so absent means OFF: no defaults are read
  an endpoint (IDO) name          -> the web service, that endpoint
  `database` or empty             -> the database
"""

import os

from . import idos

DATABASE, WEBSERVICE, OFF = "database", "webservice", "off"
OPERATIONS = ("next_co", "next_job", "customer", "defaults")


def _setting(operation):
    return {"next_co": idos.NEXT_CO_SETTING, "next_job": idos.NEXT_JOB_SETTING,
            "customer": idos.CUSTOMER_SETTING, "defaults": idos.DEFAULTS_SETTING}[operation]


def _legacy(operation):
    """The earlier web-service default when the setting is absent (None = no earlier web-service route)."""
    return {"next_co": lambda: idos.next_co()[0], "next_job": lambda: idos.next_job()[0],
            "customer": idos.customer_ido, "defaults": lambda: None}[operation]()


def route(operation, environ=None):
    """("webservice", endpoint name), ("database", None) or, for an unset `defaults` setting only, ("off", None)."""
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation: {operation}")
    env = os.environ if environ is None else environ
    name = _setting(operation)
    if name not in env:
        legacy = _legacy(operation)
        return (WEBSERVICE, legacy) if legacy else (OFF, None)
    value = (env[name] or "").strip()
    if not value or value.lower() == DATABASE:
        return DATABASE, None
    return WEBSERVICE, value


def uses_database(environ=None):
    return any(route(op, environ)[0] == DATABASE for op in OPERATIONS)


def describe(environ=None):
    """The `routes` object of the connectivity check: "database" or "webservice:<endpoint>" per operation."""
    out = {}
    for op in OPERATIONS:
        kind, name = route(op, environ)
        out[op] = f"{WEBSERVICE}:{name}" if kind == WEBSERVICE else kind
    return out
