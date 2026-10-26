"""Development mode (feature 009): the deployment names a customer order created in the ERP beforehand (DEV_TARGET_CO).

Reservations use it as the CO number, saves overwrite it, and nothing else in the ERP is touched. This module holds the
checks that make that safe: the target must exist and belong to the customer the document resolves to.
"""

from . import config, errors, idos, rowbuild
from .errors import IntegrationError


def target():
    """The development target, "" when development mode is off. Raises IntegrationError(DEV_TARGET_INVALID) for a bad value."""
    return config.dev_target_co()


def active():
    """The target for logging; never raises (an invalid setting is reported where the run starts, not in a log helper)."""
    try:
        return config.dev_target_co()
    except IntegrationError:
        return ""


def marker(target_co):
    return {"development": {"target": target_co}}


def check_target(soap, target_co, customer):
    """Refuse, before anything is written, a target that does not exist or belongs to another customer. ERP errors propagate."""
    rows = soap.read_back(idos.CO_IDO, ["CoNum", "CustNum"], rowbuild.co_filter(target_co))
    if len(rows) != 1:
        raise IntegrationError(errors.DEV_TARGET_MISSING,
                               f"development target order {target_co} was not found in the ERP; it must be created there first",
                               step="check_target")
    have = str(rows[0].get("CustNum", "")).strip()
    want = str(customer["cust_num"]).strip()
    if have != want:
        raise IntegrationError(errors.DEV_TARGET_CUSTOMER_MISMATCH,
                               f"development target order {target_co} belongs to customer {have or '(none)'}, "
                               f"but the document resolves to customer {want}", step="check_target")
