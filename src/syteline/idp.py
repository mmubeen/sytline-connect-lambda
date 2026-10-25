"""The source of the PO details: an IDP document's inference result, read through the IDP SDK.

`IdpSource.po_details(document_id)` is the ONLY way the reserve, save-intake and worker code obtain the
customer, PO number and lines (specs/004-job-number-workflow, FR-018 to FR-022). Nothing it reads is cached or
stored. A document is used only when it has finished processing, every field the save needs is present, and
every such field the IDP assessed meets its confidence threshold; otherwise the request is refused before any ERP
work, with a code that names the state or the fields (research.md R13, R17).

This is the one module that imports `idp_sdk`. Tests replace `_client_factory` with a fake.
"""

import contextlib
import logging
import os
import re

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


# how much of an SDK message the log and a debug response carry: long enough for a full "User ... is not authorized to perform ... on
# resource: <key ARN>" sentence
MESSAGE_LIMIT = 700

# what the exception type of a failed client creation usually means (the type name is all that is ever reported)
_HINTS = {
    "ModuleNotFoundError": "the IDP SDK is not in the deployed package: build with IDP_REPO set to the accelerator checkout",
    "ImportError": "the IDP SDK is not in the deployed package: build with IDP_REPO set to the accelerator checkout",
    "KeyError": "IDP_STACK_NAME is not set on the function: set the IDP settings in deploy.env and redeploy",
    "NoRegionError": "no region: set IDP_REGION (or the stack's region)",
    "ClientError": "AWS refused the call (permissions) or the service could not be reached from the subnets",
    "EndpointConnectionError": "no network path from the subnets to the IDP's AWS services (CloudFormation, S3, DynamoDB)",
    "ConnectTimeoutError": "no network path from the subnets to the IDP's AWS services (CloudFormation, S3, DynamoDB)",
}


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


@contextlib.contextmanager
def _idp_errors(document_id):
    """Refusals are passed through; anything the IDP client raises becomes DOCUMENT_NOT_FOUND or IDP_UNAVAILABLE."""
    try:
        yield
    except IntegrationError:
        raise
    except Exception as exc:
        if type(exc).__name__ == "IDPResourceNotFoundError":
            raise _refuse(errors.DOCUMENT_NOT_FOUND, f"no such document in IDP: {document_id}") from None
        # the function's own log (not the response) gets the SDK's message, shortened: it is how a denied permission or a
        # missing record is told apart; it carries no credentials
        detail = " ".join(str(exc).split())[:MESSAGE_LIMIT]
        log.warning("IDP read failed: %s: %s", type(exc).__name__, detail)
        refusal = _refuse(errors.IDP_UNAVAILABLE, "the IDP source could not be read", cause=type(exc).__name__)
        refusal.debug_detail = detail
        raise refusal from None


class DocumentRead:
    """What one read of a document gave: its state, status, metadata, and (read on first use) the confidence attributes.

    One read serves both the scan result summary and the PO details of an event run (specs/006, research R6).
    """

    def __init__(self, client, document_id, state, status, meta):
        self.client, self.document_id, self.state, self.status, self.meta = client, document_id, state, status, meta
        self._attributes = None

    def attributes(self):
        if self._attributes is None:
            with _idp_errors(self.document_id):
                self._attributes = getattr(self.client.assessment.get_confidence(document_id=self.document_id),
                                           "attributes", None) or {}
        return self._attributes


def read_document(document_id):
    """Read the document's state and metadata. Raises IntegrationError (a refusal, nothing written) when it does not
    exist, has not finished, or the IDP cannot be reached."""
    try:
        client = _client()
    except IntegrationError:
        raise
    except Exception as exc:
        # the exception type, and for a missing module its name (a module name is not sensitive); never the message
        module = getattr(exc, "name", None) if isinstance(exc, ImportError) and isinstance(getattr(exc, "name", None), str) else None
        log.warning("IDP client could not be created: %s%s", type(exc).__name__, f" (module {module})" if module else "")
        extra = {"module": module} if module else {}
        refusal = _refuse(errors.IDP_UNAVAILABLE, "the IDP source could not be reached", cause=type(exc).__name__,
                          hint=_HINTS.get(type(exc).__name__, "see the function log line 'IDP client could not be created'"),
                          **extra)
        refusal.debug_detail = " ".join(str(exc).split())[:MESSAGE_LIMIT]
        raise refusal from None
    with _idp_errors(document_id):
        status = client.document.get_status(document_id=document_id)
        state = _state(status)
        if state != COMPLETED:
            raise _refuse(errors.DOCUMENT_NOT_READY,
                          f"the document has not finished processing (it is {state}); try again when it is {COMPLETED}",
                          state=state)
        return DocumentRead(client, document_id, state, status, client.document.get_metadata(document_id=document_id))


def po_details(document_id, read=None):
    """Return {"details", "fingerprint"} for the document, or raise IntegrationError (a refusal, nothing written).

    `read` is a DocumentRead already obtained for this document (the event run reads once); otherwise it is read here.
    """
    read = read or read_document(document_id)
    with _idp_errors(document_id):
        return _details(read)


def _details(read):
    meta = read.meta
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
    low = _low_confidence(read.attributes(), used)
    if low:
        raise _refuse(errors.DOCUMENT_NEEDS_REVIEW,
                      "fields the save needs are below their confidence threshold and must be reviewed in IDP first: "
                      + ", ".join(low), fields=low)
    supplied = {i: line["job"] for i, line in enumerate(details["lines"], 1) if line.get("job")}
    return {"details": details, "fingerprint": validation.fingerprint(
        details["customer"], details["po_number"], len(details["lines"]), supplied)}


_STEP = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _resolve(fields, path):
    """The value at a result path such as `line_items[0].material_code`, or None."""
    node = fields
    for name, index in _STEP.findall(path):
        try:
            node = node[int(index)] if index else node[name]
        except (KeyError, IndexError, TypeError):
            return None
    return node


def scan_summary(read):
    """The scan result summary of a read document (specs/006 data-model): state, class, pages, issues, each assessed
    field with value and confidence, and the fields below their threshold. No images, raw text, URIs or credentials."""
    meta = read.meta
    fields = getattr(meta, "fields", None) or {}
    attributes = read.attributes()
    out_fields = []
    for path in sorted(attributes):
        entry = attributes[path]
        out_fields.append({"path": path, "value": _resolve(fields, path), "confidence": getattr(entry, "confidence", None),
                           "meets_threshold": getattr(entry, "meets_threshold", None)})
    for name in sorted(set(fields) - set(attributes)):  # extracted but not assessed
        out_fields.append({"path": name, "value": fields[name], "confidence": None, "meets_threshold": None})
    pages = getattr(read.status, "num_pages", None)
    issues = getattr(read.status, "processing_issue_count", None)
    return {"document_id": read.document_id, "state": read.state, "class": getattr(meta, "document_class", None),
            "pages": pages, "issues": issues, "fields": out_fields,
            "below_threshold": sorted(f["path"] for f in out_fields if f["meets_threshold"] is False)}
