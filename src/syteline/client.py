"""
SyteLine IDO web-service (SOAP over HTTP) client.

ASSUMPTIONS (to be confirmed read-only by task T031 against the plant network, see
specs/001-syteline-erp-integration/research.md): the SOAP operation names and payload shapes
(LoadDataSet / SaveDataSet / CallMethod). SaveDataSet's signature is confirmed (a DataSet: see `_dataset`). They are isolated in `OPS` and the XML / parse helpers so a
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
        self._with_schema = False  # `columns` asks for the DataSet's schema part to be kept

    # -- session -------------------------------------------------------------------------
    def _service(self):
        if self._zeep is None:
            import urllib3
            from requests import Session
            from zeep import Client
            from zeep.transports import Transport

            _enable_dataset_render()
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
                return self._invoke(op, args)
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

    def _invoke(self, op, args):
        service = self._service()
        if op in ("load", "save", "call") and hasattr(self._zeep, "settings"):
            # the responses hold a DataSet (schema + any) or an anyType: read them ourselves, zeep cannot parse them reliably
            with self._zeep.settings(raw_response=True):
                response = getattr(service, OPS[op])(self._token, *args)
            return _method_result(response) if op == "call" else _dataset_result(response, keep_schema=self._with_schema)
        return getattr(service, OPS[op])(self._token, *args)

    # -- operations ----------------------------------------------------------------------
    def load(self, ido, properties, filter_="", record_cap=0):
        """Return rows (list of dicts) from an IDO. Read-only. record_cap 0 = all rows."""
        raw = self._call("load", ido, ",".join(properties), filter_, "", "", record_cap)
        err = row_error(raw)  # an error reported inside a 200 answer (for example an unknown property) is a rejection
        if err:
            raise ErpRejected(err)
        return parse_rows(raw)

    read_back = load

    def columns(self, ido):
        """(property list used, [property names]) of an IDO, from the schema of a one-row read (names only, no values).

        Read-only. Tries the property list "*" and then an empty one; raises ErpRejected when the service refuses both."""
        last = None
        for plist in ("*", ""):
            self._with_schema = True
            try:
                raw = self._call("load", ido, plist, "", "", "", 1)
            except ErpRejected as exc:
                last = exc
                continue
            finally:
                self._with_schema = False
            err = row_error(raw)
            names = _schema_columns(raw) or sorted({k for row in parse_rows(raw) for k in row})
            if names and not err:
                return plist, names
            last = ErpRejected(err) if err else last
        if last:
            raise last
        return None, []

    def insert(self, ido, values):
        """Insert one row; return the stored row. Row-level rejections raise ErpRejected."""
        if not config.live_writes_enabled():
            raise LiveWritesDisabled("live ERP writes are disabled (SYTELINE_LIVE_WRITES)")
        raw = self._call("save", _dataset(ido, "Insert", values), *SAVE_TAIL, write=True)
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
        raw = self._call("save", _dataset(ido, "Update", {**key, **values}, key), *SAVE_TAIL, write=True)
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
        raw = self._call("call", ido, method, _params_xml(params), write=True)
        return [str(v) for v in (raw or [])]



# -- XML helpers --------------------------------------------------------------------------
# The real WSDL (confirmed): SaveDataSet(strSessionToken, updateDataSet: {schema, _value_1: ANY}, refreshAfterSave: boolean,
# strCustomInsert, strCustomUpdate, strCustomDelete). updateDataSet is a .NET DataSet: an XML schema, then a diffgram.
# ASSUMPTION (UNCONFIRMED against the live service): the row layout inside it (the standard DataSet diffgram, the table
# named after the IDO, rows called `IDO`; an update carries the key columns in a `diffgr:before` row). Only this function builds it.
SAVE_TAIL = (True, "", "", "")  # refreshAfterSave, strCustomInsert, strCustomUpdate, strCustomDelete

XS = "http://www.w3.org/2001/XMLSchema"
MSDATA = "urn:schemas-microsoft-com:xml-msdata"
DIFFGR = "urn:schemas-microsoft-com:xml-diffgram-v1"
TABLE = "IDO"  # the name of the table (row element) inside an IDO DataSet


def _enable_dataset_render():
    """zeep can read an `<s:element ref="s:schema"/>` part (every DataSet type of the real WSDL has one) but cannot write
    it: sending a DataSet fails with "'Schema' object has no attribute 'render'". Teach it to write the element it is given."""
    from zeep.xsd.elements import builtins

    if getattr(builtins.Schema, "_dataset_render", False):
        return

    def render(self, parent, value, render_path=None):
        if value is not None:
            parent.append(value)

    builtins.Schema.render = render
    builtins.Schema.min_occurs = builtins.Schema.max_occurs = 1
    builtins.Schema._dataset_render = True


def _dataset(ido, action, values, key=None):
    """The `updateDataSet` argument: {"schema": <xs:schema>, "_value_1": <diffgr:diffgram>} for one row of `ido`.

    The DataSet is NAMED AFTER THE IDO and its table is called `IDO` (the service reads the IDO name from the DataSet's name;
    a DataSet named "DataSet" is refused with "DataSet is not a valid IDO name")."""
    from lxml import etree

    cols = list(values)
    ns = {"xs": XS, "msdata": MSDATA, "diffgr": DIFFGR}
    schema = etree.Element(f"{{{XS}}}schema", nsmap=ns, id=ido)
    root = etree.SubElement(schema, f"{{{XS}}}element", name=ido)
    root.set(f"{{{MSDATA}}}IsDataSet", "true")
    table = etree.SubElement(etree.SubElement(etree.SubElement(root, f"{{{XS}}}complexType"), f"{{{XS}}}choice",
                                              minOccurs="0", maxOccurs="unbounded"), f"{{{XS}}}element", name=TABLE)
    seq = etree.SubElement(etree.SubElement(table, f"{{{XS}}}complexType"), f"{{{XS}}}sequence")
    for c in cols:
        etree.SubElement(seq, f"{{{XS}}}element", name=c, type="xs:string", minOccurs="0")

    diffgram = etree.Element(f"{{{DIFFGR}}}diffgram", nsmap={"msdata": MSDATA, "diffgr": DIFFGR})
    data = etree.SubElement(diffgram, ido)

    def row(parent, props, **attrs):
        el = etree.SubElement(parent, TABLE)
        for name, val in attrs.items():
            el.set(f"{{{DIFFGR if name != 'rowOrder' else MSDATA}}}{name}", val)
        for k, v in props.items():
            etree.SubElement(el, k).text = str(v)

    if action == "Insert":
        row(data, values, id=f"{TABLE}1", rowOrder="0", hasChanges="inserted")
    else:
        row(data, values, id=f"{TABLE}1", rowOrder="0", hasChanges="modified")
        row(etree.SubElement(diffgram, f"{{{DIFFGR}}}before"), key or {}, id=f"{TABLE}1", rowOrder="0")
    return {"schema": schema, "_value_1": diffgram}


class Fault(Exception):
    """A SOAP fault read from a raw response (the name is what `_call` classifies on, as for zeep's own Fault)."""


def _soap_response(response):
    """The `<...Response>` element of a raw SOAP reply; a fault raises Fault, an HTTP error status its own error."""
    from lxml import etree

    body = etree.fromstring(response.content).find("{http://schemas.xmlsoap.org/soap/envelope/}Body")
    if body is None:
        response.raise_for_status()
        raise Fault("response without a SOAP body")
    fault = body.find("{http://schemas.xmlsoap.org/soap/envelope/}Fault")
    if fault is not None:
        raise Fault((fault.findtext("faultstring") or "SOAP fault").strip())
    response.raise_for_status()
    return body[0] if len(body) else []


def _dataset_result(response, keep_schema=False):
    """The rows of a raw SOAP response holding a DataSet result, as one XML string (the schema part is dropped unless kept)."""
    from lxml import etree

    result = _soap_response(response)
    result = result[0] if len(result) else []
    parts = [etree.tostring(c, encoding="unicode") for c in result
             if isinstance(c.tag, str) and (keep_schema or etree.QName(c).localname != "schema")]
    return "<DataSet>" + "".join(parts) + "</DataSet>"


def _schema_columns(raw):
    """The column names a DataSet's schema declares for its rows (the leaf `xs:element` names), in schema order."""
    if not isinstance(raw, (str, bytes)):
        return []
    from lxml import etree

    names = []
    for el in _root(raw).iter("{http://www.w3.org/2001/XMLSchema}element"):
        name = el.get("name")
        if name and not any(etree.QName(c).localname == "complexType" for c in el) and name not in names:
            names.append(name)
    return names


def _params_xml(params):
    """CallMethod's `strMethodParameters`: ONE string, `<Parameters><Parameter>value</Parameter>...</Parameters>`
    (ASSUMPTION, UNCONFIRMED against the live service: the shape of that string)."""
    return "<Parameters>" + "".join(f"<Parameter>{escape(str(p))}</Parameter>" for p in params) + "</Parameters>"


def _method_result(response):
    """The output parameters of a raw CallMethod response (its `strMethodParameters`, same shape as the request's)."""
    from lxml import etree

    for el in _soap_response(response):
        if isinstance(el.tag, str) and etree.QName(el).localname == "strMethodParameters" and (el.text or "").strip():
            return [(p.text or "") for p in etree.fromstring(el.text.encode("utf-8")).iter("Parameter")]
    return []


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
