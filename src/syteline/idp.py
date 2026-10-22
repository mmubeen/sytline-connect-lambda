"""The source of the PO details: an IDP document's inference result, read through the IDP SDK.

`IdpSource.po_details(document_id)` is the ONLY way the reserve, save-intake and worker code obtain the
customer, PO number and lines (specs/004-job-number-workflow, FR-018 to FR-022). Nothing it reads is cached or
stored. A document is used only when it has finished processing, every field the save needs is present, and
every such field the IDP assessed meets its confidence threshold; otherwise the request is refused before any ERP
work, with a code that names the state or the fields (research.md R13, R17).

This is the one module that imports `idp_sdk`. Tests replace `_client_factory` with a fake.
"""

import logging
import os

from . import errors, idp_fields, validation
from .errors import IntegrationError

log = logging.getLogger(__name__)

# Overridable factory (tests inject a fake with the same document / assessment surface as IDPClient).
_client_factory = None

COMPLETED = "COMPLETED"


def _client():
    if _client_factory:
        return _client_factory()
    from idp_sdk import IDPClient  # imported here: only the functions that read documents need the SDK

    return IDPClient(stack_name=os.environ["IDP_STACK_NAME"], region=os.environ.get("IDP_REGION") or None)


def _refuse(code, message, **extra):
    return IntegrationError(code, message, step="document", **extra)


def _state(status):
    s = getattr(status, "status", status)
    return str(getattr(s, "value", s))


def _low_confidence(attributes, used):
    """Result paths (among those the save reads) whose assessed confidence is below the threshold.

    A path is looked up as written, then by its top-level name; a field the IDP did not assess has no entry and is
    not treated as low (nothing says it is).
    """
    low = []
    for path in used:
        entry = attributes.get(path)
        if entry is None:
            entry = attributes.get(path.split(".")[0].split("[")[0])
        if entry is not None and getattr(entry, "meets_threshold", True) is False:
            low.append(path)
    return sorted(set(low))


def po_details(document_id):
    """Return {"details", "fingerprint"} for the document, or raise IntegrationError (a refusal, nothing written)."""
    try:
        client = _client()
    except IntegrationError:
        raise
    except Exception as exc:
        log.warning("IDP client could not be created: %s", type(exc).__name__)
        raise _refuse(errors.IDP_UNAVAILABLE, "the IDP source could not be reached") from None
    try:
        return _read(client, document_id)
    except IntegrationError:
        raise
    except Exception as exc:
        if type(exc).__name__ == "IDPResourceNotFoundError":
            raise _refuse(errors.DOCUMENT_NOT_FOUND, f"no such document in IDP: {document_id}") from None
        log.warning("IDP read failed: %s", type(exc).__name__)
        raise _refuse(errors.IDP_UNAVAILABLE, "the IDP source could not be read") from None


def _read(client, document_id):
    state = _state(client.document.get_status(document_id=document_id))
    if state != COMPLETED:
        raise _refuse(errors.DOCUMENT_NOT_READY,
                      f"the document has not finished processing (it is {state}); try again when it is {COMPLETED}",
                      state=state)
    meta = client.document.get_metadata(document_id=document_id)
    expected = idp_fields.document_class()
    if expected and getattr(meta, "document_class", None) != expected:
        raise _refuse(errors.DOCUMENT_INCOMPLETE,
                      f"the document is a {getattr(meta, 'document_class', None)!r}, expected {expected!r}",
                      fields=["document_class"])
    details, missing, used = idp_fields.map_fields(getattr(meta, "fields", None))
    if missing:
        raise _refuse(errors.DOCUMENT_INCOMPLETE,
                      "the document result is missing or has unreadable fields the save needs: " + ", ".join(missing),
                      fields=missing)
    if len(details["lines"]) > idp_fields.MAX_LINES:
        raise _refuse(errors.TOO_MANY_LINES, f"the document has more than {idp_fields.MAX_LINES} lines",
                      fields=["lines"])
    attributes = getattr(client.assessment.get_confidence(document_id=document_id), "attributes", None) or {}
    low = _low_confidence(attributes, used)
    if low:
        raise _refuse(errors.DOCUMENT_NEEDS_REVIEW,
                      "fields the save needs are below their confidence threshold and must be reviewed in IDP first: "
                      + ", ".join(low), fields=low)
    return {"details": details, "fingerprint": validation.fingerprint(
        details["customer"], details["po_number"], len(details["lines"]))}
