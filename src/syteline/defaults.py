"""The customer's default terms code and ship-via code, for the new order's header.

Taken from the customer's most recent order: first an order for the same ship-to address, otherwise any order of the
customer (the PO OCR demo's rule). The route follows the `defaults` setting (syteline.routes): the database, or the web
service when an endpoint (an order collection) is configured; a failure never switches route.

Never blocks a save: any problem, or a customer without orders, returns no defaults and a warning that the caller shows.
"""

import logging

from . import config, idos, routes, rowbuild, steplog
from .errors import ErpError, IntegrationError

log = logging.getLogger(__name__)

NO_ORDER = "customer defaults not available: no previous order for this customer"


def _co_key(value):
    text = str(value or "").strip()
    return (0, int(text)) if text.isdigit() else (1, text)


def _from_service(customer, soap, collection):
    """Highest CoNum of the customer's orders at the same ship-to, else of any of its orders (client-side, so the
    web service only has to filter on the customer)."""
    props = idos.CO_HEADER_PROPS
    rows = soap.load(collection, ["CoNum", "CustNum", "CustSeq", props["terms_code"], props["ship_code"]],
                     idos.filter_eq("CustNum", rowbuild.rjust(customer["cust_num"], rowbuild.CUST_NUM_WIDTH)))
    seq = str(customer.get("cust_seq", "")).strip()

    def newest(candidates):
        for row in sorted(candidates, key=lambda r: _co_key(r.get("CoNum")), reverse=True):
            terms, ship = (str(row.get(props["terms_code"]) or "").strip() or None,
                           str(row.get(props["ship_code"]) or "").strip() or None)
            if terms or ship:
                return terms, ship
        return None

    same = newest([r for r in rows if seq != "" and str(r.get("CustSeq", "")).strip() == seq])
    if same:
        return same + ("same_ship_to",)
    other = newest(rows)
    return other + ("any_order",) if other else None


def read(customer, soap, cfg):
    """Return (defaults | None, warning | None); defaults is {"terms_code", "ship_code", "source"}."""
    try:
        kind, name = routes.route("defaults")
        if kind == routes.OFF:  # the setting is absent: no defaults are read (the earlier behaviour)
            return None, None
        if kind == routes.WEBSERVICE:
            found = _from_service(customer, soap, name or idos.DEFAULTS_IDO_DEFAULT)
        else:
            from .sqlserver import SqlServer

            started = steplog.now()
            with SqlServer(cfg if cfg is not None else config.load_config()) as db:
                found = db.latest_order_codes(customer["cust_num"], customer.get("cust_seq"))
            steplog.step("customer_defaults", started, "found" if found else "none")
    except IntegrationError as exc:  # e.g. DATABASE_NOT_CONFIGURED
        return None, f"customer defaults not available: {exc.err['message']}"
    except ErpError as exc:
        log.warning("customer defaults could not be read: %s", type(exc).__name__)
        return None, f"customer defaults not available: {exc}"
    except Exception as exc:  # never blocks a save
        log.warning("customer defaults could not be read: %s", type(exc).__name__)
        return None, f"customer defaults not available: {type(exc).__name__}"
    if not found:
        return None, NO_ORDER
    terms, ship, source = found
    return {"terms_code": terms, "ship_code": ship, "source": source}, None
