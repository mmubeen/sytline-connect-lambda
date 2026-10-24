"""Mock of the SyteLine IDO web service (specs/008 R5): a WSGI app with in-memory IDO collections.

Speaks the SOAP operations src/syteline/client.py uses (CreateSessionToken, LoadDataSet, SaveDataSet, CallMethod) so the
function's real client, zeep and HTTP handling are exercised. Every request is recorded (secrets and tokens are not), and the
`erp.*` faults of the control API can be switched on. The control endpoints live under /__erp__/.
"""

import itertools
import json
import os
import re
import threading
import time
import uuid
from xml.sax.saxutils import escape

from lxml import etree
from werkzeug.wrappers import Request, Response

from edge import models

SERVICE_PATH = "/IDORequestService/IDOWebService.asmx"
NS = "http://tempuri.org/"
HERE = os.path.dirname(__file__)


def _unquote(v):
    v = v.strip()
    return v[1:-1].replace("''", "'") if v.startswith("'") else v


def _like_to_regex(pattern):
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if c == "[" and pattern[i:i + 3] in ("[%]", "[_]", "[[]"):
            out += re.escape(pattern[i + 1])
            i += 3
            continue
        out += ".*" if c == "%" else re.escape(c)
        i += 1
    return "^" + out + "$"


def _match(row, cond):
    m = re.match(r"^(\w+)\s+(=|>=|LIKE)\s+(.*)$", cond.strip())
    if not m:
        raise ValueError(f"unsupported filter condition: {cond}")
    prop, op, raw = m.groups()
    have, want = str(row.get(prop, "")).strip(), _unquote(raw)
    if op == "=":
        return have.upper() == want.upper()
    if op == ">=":
        return have >= want
    return re.match(_like_to_regex(want), have, re.I) is not None


class SoapFault(Exception):
    pass


class ErpMock:
    def __init__(self, user, password, config="CCT"):
        self.user, self.password, self.config = user, password, config
        self._lock = threading.RLock()
        self.log = models.CallLog()
        self.faults = models.FaultSet()
        self.reset()

    # -- state -----------------------------------------------------------------------------
    def reset(self):
        with self._lock:
            self._rows = {}
            self._sessions = set()
            self._counters = {"co": 100, "job": 5000, "rp": 1, "note": 1}
            self.log.clear()
            self.faults.clear()

    def load(self, data):
        """Seed from a scenario's erp.json: {"collections": {ido: [row, ...]}, "counters": {"co": n, "job": n}}."""
        with self._lock:
            for ido, rows in (data.get("collections") or {}).items():
                for row in rows:
                    self._add(ido, dict(row))
            self._counters.update({k: int(v) for k, v in (data.get("counters") or {}).items()})

    def rows(self, ido):
        with self._lock:
            return [dict(r) for r in self._rows.get(ido, [])]

    def _add(self, ido, row):
        row.setdefault("RowPointer", f"rp-{self._counters['rp']}")
        self._counters["rp"] += 1
        self._rows.setdefault(ido, []).append(row)
        return row

    def set_fault(self, fault_id, params=None, remaining=None):
        self.faults.add(fault_id, params, remaining)

    def clear_faults(self):
        return self.faults.clear()

    def faults_active(self):
        return self.faults.active()

    def calls_since(self, since):
        return self.log.query(since=since)

    # -- WSGI ------------------------------------------------------------------------------
    def __call__(self, environ, start_response):
        request = Request(environ)
        if request.path.startswith("/__erp__/"):
            response = self._control(request)
        elif request.path == SERVICE_PATH and request.method == "GET":
            response = self._wsdl(request)
        elif request.path == SERVICE_PATH and request.method == "POST":
            response = self._soap(request)
        else:
            response = Response("not found", 404)
        return response(environ, start_response)

    def _wsdl(self, request):
        with open(os.path.join(HERE, "service.wsdl")) as f:
            text = f.read().replace("@LOCATION@", request.host_url.rstrip("/") + SERVICE_PATH)
        return Response(text, 200, {"Content-Type": "text/xml; charset=utf-8"})

    def _control(self, request):
        name = request.path[len("/__erp__/"):]
        body = request.get_json(silent=True) or {}
        if name == "health":
            return _json({"ok": True})
        if name == "reset":
            self.reset()
            return _json({"reset": True})
        if name == "load":
            self.load(body)
            return _json({"loaded": True})
        if name == "calls":
            return _json(self.calls_since(int(request.args.get("since", 0))))
        if name == "fault":
            try:
                self.set_fault(body["id"], body.get("params"), body.get("remaining"))
            except (KeyError, ValueError) as exc:
                return _json({"error": str(exc)}, 400)
            return _json({"fault_id": body["id"]})
        if name == "clear_faults":
            return _json({"cleared": self.clear_faults()})
        return _json({"error": "no such control route"}, 404)

    # -- SOAP ------------------------------------------------------------------------------
    def _soap(self, request):
        try:
            root = etree.fromstring(request.get_data())
            op_el = root.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")[0]
        except (etree.XMLSyntaxError, IndexError, TypeError):
            return _fault("malformed SOAP request", 400)
        op = etree.QName(op_el).localname
        args = [(etree.QName(c).localname, c) for c in op_el]
        values = self._values(args)
        fault = None
        summary = self._summary(op, args)
        try:
            with self._lock:
                for fid in ("erp.unreachable", "erp.slow", "erp.session_expired"):
                    if fid == "erp.session_expired" and op == "CreateSessionToken":
                        continue
                    f = self.faults.take(fid)
                    if f:
                        fault = fid
                        if fid == "erp.unreachable":
                            self.log.add("erp", op, summary, fault=fid, status=503)
                            return Response("Service Unavailable", 503)
                        if fid == "erp.slow":
                            time.sleep(float(f["params"].get("delay_ms", 5000)) / 1000.0)
                        else:
                            self.log.add("erp", op, summary, fault=fid, status=500)
                            return _fault("Session token expired or invalid")
                result = self._dispatch(op, values, summary)
        except SoapFault as exc:
            self.log.add("erp", op, summary, fault=fault, status=500)
            return _fault(str(exc))
        self.log.add("erp", op, summary, fault=fault, status=200)
        return _envelope(op, result)

    @staticmethod
    def _values(args):
        """Positional values; a name that repeats (the method parameters) becomes one list."""
        out, index = [], {}
        for name, el in args:
            if name == "parameters":
                if name not in index:
                    index[name] = len(out)
                    out.append([])
                out[index[name]].append(el.text or "")
            else:
                out.append(ErpMock._arg(el))
        return out

    @staticmethod
    def _arg(el):
        kids = [k for k in el if isinstance(k.tag, str)]
        return [k.text or "" for k in kids] if kids else (el.text or "")

    @staticmethod
    def _summary(op, args):
        """What the call log keeps: names and values, never the password or the session token."""
        out = {}
        for name, c in args:
            value = "***" if name in ("password", "sessionToken") else (c.text if len(c) == 0 else "[list]")
            out[name] = out[name] + [value] if name == "parameters" and name in out else \
                ([value] if name == "parameters" else value)
        return out

    def _check(self, token):
        if token not in self._sessions:
            raise SoapFault("Session token expired or invalid")

    def _dispatch(self, op, v, summary):
        if op == "CreateSessionToken":
            user, password, config = v[:3]
            if user != self.user or password != self.password:
                return ("CreateSessionTokenResult", "")
            token = uuid.uuid4().hex
            self._sessions.add(token)
            return ("CreateSessionTokenResult", token)
        self._check(v[0])
        if op == "LoadDataSet":
            _, ido, props, flt, _order, _post, cap = v
            rows = self._select(ido, flt)
            wanted = [p for p in props.split(",") if p and p != "*"]
            rows = [{k: r[k] for k in wanted if k in r} if wanted else r for r in rows]
            if int(cap or 0):
                rows = rows[:int(cap)]
            return ("LoadDataSetResult", _rows_xml(ido, rows))
        if op == "SaveDataSet":
            return ("SaveDataSetResult", self._save(v[1]))
        if op == "CallMethod":
            return ("CallMethodResult", self._call(v[1], v[2], v[3] if len(v) > 3 else []))
        raise SoapFault(f"unknown operation {op}")

    def _select(self, ido, flt):
        rows = [dict(r) for r in self._rows.get(ido, [])]
        for cond in [c for c in re.split(r"\s+AND\s+", flt or "") if c.strip()]:
            try:
                rows = [r for r in rows if _match(r, cond)]
            except ValueError as exc:
                raise SoapFault(str(exc))
        return rows

    def _save(self, changes):
        if self.faults.take("erp.row_rejected"):
            self.log.add("erp", "SaveDataSet", {"note": "rejected by fault"}, fault="erp.row_rejected", status=200)
            return '<IDOChanges><Row Error="Row rejected by the mock ERP (erp.row_rejected)"/></IDOChanges>'
        out = []
        for el in etree.fromstring(changes.encode("utf-8")):
            ido, action = etree.QName(el).localname, el.get("Action")
            values = {etree.QName(c).localname: (c.text or "") for c in el}
            if action == "Insert":
                stored = self._add(ido, values)
            elif action == "Update":
                stored = self._best_match(ido, values)
                stored.update(values)
            else:
                raise SoapFault(f"unsupported action {action}")
            out.append(_row_xml(ido, stored))
        return "<IDOChanges>" + "".join(out) + "</IDOChanges>"

    def _best_match(self, ido, values):
        best, score = None, 0
        for r in self._rows.get(ido, []):
            s = sum(1 for k, v in values.items() if str(r.get(k)) == str(v))
            if s > score:
                best, score = r, s
        if best is None:
            raise SoapFault(f"{ido} row not found for update")
        return best

    def _call(self, ido, method, params):
        if method == "NextCoSp":
            n = self._counters["co"]
            self._counters["co"] += 1
            return [str(n)]
        if method == "NextWOSp":
            n = self._counters["job"]
            self._counters["job"] += 1
            return [str(n)]
        if method == "CreateSpecificNoteSp":
            _obj, rp, desc, text, flag = (params + [""] * 5)[:5]
            token = str(self._counters["note"])
            self._counters["note"] += 1
            self._add("SpecificNotes", {"SpecificNoteToken": token, "NoteContent": text, "NoteDesc": desc,
                                        "CreateDate": time.strftime("%Y-%m-%d"), "CreatedBy": "mock"})
            self._add("ObjectNotes", {"SpecificNoteToken": token, "RefRowPointer": rp, "NhNoteFlag": str(flag)})
            return []
        raise SoapFault(f"unknown method {method}")


def _row_xml(ido, row):
    return f"<{ido}>" + "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in row.items()) + f"</{ido}>"


def _rows_xml(ido, rows):
    return "<DataSet>" + "".join(_row_xml(ido, r) for r in rows) + "</DataSet>"


def _envelope(op, result):
    name, value = result
    if isinstance(value, list):
        inner = "".join(f"<string>{escape(x)}</string>" for x in value)
    else:
        inner = escape(value)
    xml = ('<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
           f'<soap:Body><{op}Response xmlns="{NS}"><{name}>{inner}</{name}></{op}Response></soap:Body></soap:Envelope>')
    return Response(xml, 200, {"Content-Type": "text/xml; charset=utf-8"})


def _fault(message, status=500):
    xml = ('<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
           f"<soap:Body><soap:Fault><faultcode>soap:Server</faultcode><faultstring>{escape(message)}</faultstring>"
           "</soap:Fault></soap:Body></soap:Envelope>")
    return Response(xml, status, {"Content-Type": "text/xml; charset=utf-8"})


def _json(body, status=200):
    return Response(json.dumps(body, default=str), status, {"Content-Type": "application/json"})


def main():
    """Container entry. The login is the function's own settings (SYTELINE_USER from the template); the password is the run's
    generated one, read from the run directory (MOCK_RUN_DIR/passwords.json) unless ERP_PASS is set."""
    from werkzeug.serving import run_simple

    password = os.environ.get("ERP_PASS")
    if not password:
        with open(os.path.join(os.environ.get("MOCK_RUN_DIR", "/run/mock"), "passwords.json")) as f:
            password = json.load(f)["SYTELINE_PASS"]
    mock = ErpMock(os.environ.get("ERP_USER", "mock-user"), password, os.environ.get("ERP_CONFIG", "CCT"))
    run_simple("0.0.0.0", int(os.environ.get("ERP_PORT", "8080")), mock, threaded=True)


if __name__ == "__main__":
    main()
