"""
SyteLine IDO web-service (SOAP over HTTP) client.

ASSUMPTIONS (to be confirmed read-only by task T031 against the plant network, see
specs/001-syteline-erp-integration/research.md): the SOAP operation names and payload shapes
(LoadDataSet / SaveDataSet / CallMethod). They are isolated in `OPS` and the `_xml_*` / parse helpers so a
correction touches only this file.

Safety rules (FR-004, FR-016):
  * `insert`, `update` and `invoke` (a method call may write) refuse to run unless live writes are enabled.
  * HTTP 200 is NOT acceptance: row-level rejections inside the response raise ErpRejected.
  * A transport failure after a write request may have been sent raises ErpUncertain, never a
    plain failure. There is intentionally no delete operation.
"""

import logging
from xml.sax.saxutils import escape

from . import config
from .errors import (ErpError, ErpRejected, ErpUncertain, ErpUnreachable,  # noqa: F401
                     LiveWritesDisabled)

log = logging.getLogger(__name__)

OPS = {"load": "LoadDataSet", "save": "SaveDataSet", "call": "CallMethod"}


def _looks_expired(exc):
    msg = str(exc).lower()
    return "session" in msg and ("expired" in msg or "invalid" in msg or "token" in msg)


def _definitely_not_sent(exc):
    """True when the failure happened before the request left (building it, or connecting), so it cannot have been processed."""
    if isinstance(exc, (AttributeError, TypeError, ValueError, KeyError)):  # a bug while serializing the request
        return True
    try:
        import requests

        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return True
    except ImportError:  # pragma: no cover
        pass
    text = f"{type(exc).__name__} {exc}"
    return "Failed to establish a new connection" in text or "NewConnectionError" in text


class SoapIdoClient:
    def __init__(self, cfg, timeout=10.0, verify=False, zeep_client=None):
        self._cfg = cfg
        self._timeout = timeout
        self._verify = verify
        self._zeep = zeep_client
        self._token = None

    # -- session -------------------------------------------------------------------------
    def _service(self):
        if self._zeep is None:
            import urllib3
            from requests import Session
            from zeep import Client
            from zeep.transports import Transport

            if not self._verify:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            session = Session()
            session.verify = self._verify
            transport = Transport(session=session, cache=None, timeout=self._timeout,
                                  operation_timeout=self._timeout)
            self._zeep = Client(self._cfg["SYTELINE_WSDL"], transport=transport)
        return self._zeep.service

    def connect(self):
        try:
            tok = self._service().CreateSessionToken(
                self._cfg["SYTELINE_USER"], self._cfg["SYTELINE_PASS"], self._cfg["SYTELINE_CONFIG"]
            )
        except Exception as exc:
            raise ErpUnreachable(f"{type(exc).__name__}: {exc}") from None
        if not tok:
            raise ErpRejected("login failed: empty session token returned")
        self._token = tok

    def _call(self, op, *args, write=False):
        """Call a SOAP operation, re-logging in once if the session has expired.

        write=True: a transport failure after the request may have been sent is ErpUncertain.
        """
        if self._token is None:
            self.connect()
        for attempt in (0, 1):
            try:
                return getattr(self._service(), OPS[op])(self._token, *args)
            except Exception as exc:
                if attempt == 0 and _looks_expired(exc):
                    log.info("session expired; re-authenticating once")
                    self.connect()  # the expired request was not processed, so a retry is safe
                    continue
                if type(exc).__name__ == "Fault":
                    raise ErpRejected(str(exc)) from None
                if write and not _definitely_not_sent(exc):
                    raise ErpUncertain(f"{type(exc).__name__}: {exc}") from None
                raise ErpUnreachable(f"{type(exc).__name__}: {exc}") from None

    # -- operations ----------------------------------------------------------------------
    def load(self, ido, properties, filter_="", record_cap=0):
        """Return rows (list of dicts) from an IDO. Read-only. record_cap 0 = all rows."""
        raw = self._call("load", ido, ",".join(properties), filter_, "", "", record_cap)
        return parse_rows(raw)

    read_back = load

    def insert(self, ido, values):
        """Insert one row; return the stored row. Row-level rejections raise ErpRejected."""
        if not config.live_writes_enabled():
            raise LiveWritesDisabled("live ERP writes are disabled (SYTELINE_LIVE_WRITES)")
        raw = self._call("save", _xml_changes(ido, "Insert", values), write=True)
        err = row_error(raw)
        if err:
            raise ErpRejected(err)
        rows = parse_rows(raw)
        return rows[0] if rows else dict(values)

    def update(self, ido, key, values):
        """Update one existing row (identified by `key`, a property -> value dict); return the stored row.

        Same safety classification as `insert`: gated by live writes, a row-level rejection raises ErpRejected,
        a transport failure after the request may have been sent raises ErpUncertain. ASSUMPTION (UNCONFIRMED,
        discovery task): an Update change carries the key properties plus the changed ones.
        """
        if not config.live_writes_enabled():
            raise LiveWritesDisabled("live ERP writes are disabled (SYTELINE_LIVE_WRITES)")
        raw = self._call("save", _xml_changes(ido, "Update", {**key, **values}), write=True)
        err = row_error(raw)
        if err:
            raise ErpRejected(err)
        rows = parse_rows(raw)
        return rows[0] if rows else {**key, **values}

    def invoke(self, ido, method, params):
        """Call an IDO method (e.g. NextCoSp) through the web service; return its output values.

        Every call may write, so it is gated by live writes. A fault is a definite rejection, a
        transport failure after the request may have been sent is ErpUncertain.
        """
        if not config.live_writes_enabled():
            raise LiveWritesDisabled("live ERP writes are disabled (SYTELINE_LIVE_WRITES)")
        raw = self._call("call", ido, method, [str(p) for p in params], write=True)
        return [str(v) for v in (raw or [])]



# -- XML helpers (shape assumed, see module docstring) -----------------------------------
def _xml_changes(ido, action, values):
    props = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in values.items())
    return f"<IDOChanges><{ido} Action=\"{action}\">{props}</{ido}></IDOChanges>"


def _root(raw):
    from lxml import etree

    if not isinstance(raw, (str, bytes)):
        raw = etree.tostring(raw)
    return etree.fromstring(raw if isinstance(raw, bytes) else raw.encode("utf-8"))


def row_error(raw):
    """Return the ERP's message if the response carries a row-level error (even on HTTP 200)."""
    if raw is None or raw == "":
        return None
    from lxml import etree

    root = _root(raw)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for name, val in el.attrib.items():
            local = etree.QName(name).localname
            if local in ("Error", "RowError", "rowError") and val:
                return val
            if local == "hasErrors" and val.lower() == "true":
                return el.attrib.get("{urn:schemas-microsoft-com:xml-diffgram-v1}Error") or "row rejected by ERP"
        if etree.QName(el).localname in ("Error", "RowError", "Infobar") and (el.text or "").strip():
            return el.text.strip()
    return None


def parse_rows(raw):
    """Parse a DataSet/diffgram-style XML string into a list of {property: value} dicts.

    A row is any element whose children are all leaf elements.
    """
    if raw is None or raw == "":
        return []
    from lxml import etree

    rows = []
    for el in _root(raw).iter():
        kids = [c for c in el if isinstance(c.tag, str)]
        if kids and all(len(c) == 0 for c in kids):
            rows.append({etree.QName(c).localname: (c.text or "") for c in kids})
    return rows
