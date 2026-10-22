"""Error codes, exceptions and response helpers (see specs/001-syteline-erp-integration/contracts)."""

VALIDATION = "VALIDATION"
NOT_FOUND = "NOT_FOUND"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
UNKNOWN_ACTION = "UNKNOWN_ACTION"
REQUEST_REF_CONFLICT = "REQUEST_REF_CONFLICT"
CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND"
CUSTOMER_AMBIGUOUS = "CUSTOMER_AMBIGUOUS"
LIVE_WRITES_DISABLED = "LIVE_WRITES_DISABLED"
ERP_UNREACHABLE = "ERP_UNREACHABLE"
ERP_REJECTED = "ERP_REJECTED"
READBACK_MISMATCH = "READBACK_MISMATCH"
UNCERTAIN_OUTCOME = "UNCERTAIN_OUTCOME"
STALE_RUNNING = "STALE_RUNNING"
TIME_BUDGET_EXHAUSTED = "TIME_BUDGET_EXHAUSTED"
STORE_UNAVAILABLE = "STORE_UNAVAILABLE"
INSECURE_TRANSPORT = "INSECURE_TRANSPORT"
SECRET_STORE_UNREACHABLE = "SECRET_STORE_UNREACHABLE"
# job reservation workflow (feature 004)
TOO_MANY_LINES = "TOO_MANY_LINES"
RESERVATION_USED = "RESERVATION_USED"
RESERVATION_MISMATCH = "RESERVATION_MISMATCH"
RESERVATION_FORBIDDEN = "RESERVATION_FORBIDDEN"
RESERVATION_NOT_READY = "RESERVATION_NOT_READY"
# document source (the IDP result the PO details come from)
DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
DOCUMENT_NOT_READY = "DOCUMENT_NOT_READY"
DOCUMENT_INCOMPLETE = "DOCUMENT_INCOMPLETE"
DOCUMENT_NEEDS_REVIEW = "DOCUMENT_NEEDS_REVIEW"
DOCUMENT_CHANGED = "DOCUMENT_CHANGED"
IDP_UNAVAILABLE = "IDP_UNAVAILABLE"


class ErpError(Exception):
    """Base class for ERP-side failures."""


class ErpRejected(ErpError):
    """Definite: the ERP refused the request (e.g. a row-level rejection inside an HTTP 200)."""


class ErpUncertain(ErpError):
    """Uncertain: a request was sent but the outcome is unknown (timeout, transport error)."""


class ErpUnreachable(ErpError):
    """The ERP could not be reached (nothing was sent that could have been written)."""


class InsecureTransport(Exception):
    """The ERP address (or the service address declared in its WSDL) is plain http:// while HTTPS is required."""


class SecretStoreUnavailable(Exception):
    """The credentials secret could not be read from Secrets Manager (unreachable, denied or missing)."""


class LiveWritesDisabled(Exception):
    """A write was attempted while SYTELINE_LIVE_WRITES is not enabled (FR-016)."""


def error(code, message, field=None, step=None):
    e = {"code": code, "message": message}
    if field:
        e["field"] = field
    if step:
        e["step"] = step
    return e


def failure(action, *errors, **extra):
    out = {"ok": False, "action": action, "errors": list(errors)}
    out.update(extra)
    return out


class IntegrationError(Exception):
    """Raised by action modules; carries a structured error and optional extra response fields."""

    def __init__(self, code, message, step=None, **extra):
        super().__init__(message)
        self.err = error(code, message, step=step)
        self.extra = extra
