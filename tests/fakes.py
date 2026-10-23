"""In-memory fakes: web-service (SOAP IDO) client, DynamoDB job table, Lambda invoker."""

import itertools
import re

from syteline import config, idos
from syteline.errors import ErpRejected, ErpUncertain, ErpUnreachable, LiveWritesDisabled


def _unquote(v):
    v = v.strip()
    return v[1:-1].replace("''", "'") if v.startswith("'") else v


def _like_to_regex(pattern):
    out = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "[" and pattern[i:i + 3] in ("[%]", "[_]", "[[]"):
            out += re.escape(pattern[i + 1]); i += 3; continue
        out += ".*" if c == "%" else re.escape(c)
        i += 1
    return "^" + out + "$"


def _match(row, cond):
    m = re.match(r"^(\w+)\s+(=|>=|LIKE)\s+(.*)$", cond.strip())
    assert m, f"unsupported filter condition: {cond}"
    prop, op, raw = m.groups()
    have = str(row.get(prop, "")).strip()
    want = _unquote(raw)
    if op == "=":
        return have.upper() == want.upper()
    if op == ">=":
        return have >= want
    return re.match(_like_to_regex(want), have, re.I) is not None


class FakeSoap:
    """Implements the SoapIdoClient surface: load / insert / invoke / read_back. There is NO delete.

    scenarios: {("insert", ido, nth) | ("invoke", method, nth): action}; nth is 1-based per name.
    action:
      "reject"          -> ErpRejected, nothing stored/executed
      "timeout_after"   -> executed/stored, then ErpUncertain
      "timeout_before"  -> not executed, ErpUncertain
      "unreachable"     -> ErpUnreachable (definitely not sent)
    read_fail makes every load raise ErpUnreachable; readback_fail only the non-customer loads; corrupt={(ido, prop): value} alters read-back values.
    """

    def __init__(self, scenarios=None, read_fail=False, corrupt=None, customers=None, enforce_live_gate=True,
                 readback_fail=False):
        self.rows = {}
        self.scenarios = dict(scenarios or {})
        self.read_fail = read_fail
        self.readback_fail = readback_fail  # fail every read except the customer lookup
        self.corrupt = dict(corrupt or {})
        self.calls = []
        self._count = {}
        self._rp = itertools.count(1)
        self._co = itertools.count(100)
        self._job = itertools.count(5000)
        self._token = itertools.count(1)
        self._gate = enforce_live_gate
        for c in customers or []:
            self.rows.setdefault(idos.customer_ido(), []).append({
                "CustNum": c["cust_num"], "CustSeq": c.get("cust_seq", 0), "Name": c.get("name", ""),
                "Addr_1": c.get("address1", ""), "City": c.get("city", ""), "State": c.get("state", ""),
                "Zip": c.get("postal_code", "")})

    # -- helpers ---------------------------------------------------------------------
    def add_record(self, ido, **values):
        row = dict(values, RowPointer=f"rp-{next(self._rp)}")
        self.rows.setdefault(ido, []).append(row)
        return row

    def _gate_check(self):
        if self._gate and not config.live_writes_enabled():
            raise LiveWritesDisabled("live writes disabled")

    def _scenario(self, op, name):
        n = self._count[(op, name)] = self._count.get((op, name), 0) + 1
        return self.scenarios.get((op, name, n))

    # -- surface ---------------------------------------------------------------------
    def insert(self, ido, values):
        self._gate_check()  # refused before it counts as a write
        self.calls.append(("insert", ido))
        action = self._scenario("insert", ido)
        if action == "reject":
            raise ErpRejected(f"{ido} row rejected by ERP")
        if action == "timeout_before":
            raise ErpUncertain("timeout")
        if action == "unreachable":
            raise ErpUnreachable("connect failed")
        row = self.add_record(ido, **values)
        if action == "timeout_after":
            raise ErpUncertain("timeout after insert")
        return row

    def update(self, ido, key, values):
        """Update the stored row(s) matching `key`; scenarios as for insert, keyed ("update", ido, nth)."""
        self._gate_check()
        self.calls.append(("update", ido))
        action = self._scenario("update", ido)
        if action == "reject":
            raise ErpRejected(f"{ido} update rejected by ERP")
        if action == "timeout_before":
            raise ErpUncertain("timeout")
        if action == "unreachable":
            raise ErpUnreachable("connect failed")
        rows = [r for r in self.rows.get(ido, []) if all(str(r.get(k, "")) == str(v) for k, v in key.items())]
        if not rows:
            raise ErpRejected(f"{ido} row not found for update")
        for r in rows:
            r.update(values)
        if action == "timeout_after":
            raise ErpUncertain("timeout after update")
        return dict(rows[0])

    def invoke(self, ido, method, params):
        self._gate_check()  # refused before it counts as a write
        self.calls.append(("invoke", method))
        action = self._scenario("invoke", method)
        if action == "reject":
            raise ErpRejected(f"{method} rejected")
        if action == "timeout_before":
            raise ErpUncertain("timeout")
        if action == "unreachable":
            raise ErpUnreachable("connect failed")
        if method == idos.NEXT_CO_METHOD:
            out = [str(next(self._co))]
        elif method == idos.NEXT_JOB_METHOD:
            out = [str(next(self._job))]
        elif method == idos.CREATE_NOTE_METHOD:
            _obj, rp, desc, text, flag = params
            token = next(self._token)
            self.rows.setdefault(idos.NOTE_IDO, []).append({
                "SpecificNoteToken": str(token), "NoteContent": text, "NoteDesc": desc,
                "CreateDate": "2026-10-20", "CreatedBy": "svc"})
            self.rows.setdefault(idos.OBJECT_NOTE_IDO, []).append({
                "SpecificNoteToken": str(token), "RefRowPointer": rp, "NhNoteFlag": str(flag)})
            out = []
        else:
            raise ErpRejected(f"unknown method {method}")
        if action == "timeout_after":
            raise ErpUncertain("timeout after execution")
        return out

    def load(self, ido, properties, filter_="", record_cap=0):
        self.calls.append(("load", ido))
        if self.read_fail or (self.readback_fail and ido != idos.customer_ido()):
            raise ErpUnreachable("read failed")
        rows = [dict(r) for r in self.rows.get(ido, [])]
        for cond in [c for c in re.split(r"\s+AND\s+", filter_) if c.strip()] if filter_ else []:
            rows = [r for r in rows if _match(r, cond)]
        for (c_ido, prop), value in self.corrupt.items():
            if c_ido == ido:
                for r in rows:
                    r[prop] = value
        return rows[:record_cap] if record_cap else rows

    read_back = load

    def count(self, ido):
        return len(self.rows.get(ido, []))

    def writes(self):
        return [c for c in self.calls if c[0] in ("insert", "invoke", "update")]


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


import threading as _threading_mod  # noqa: E402

_FAKE_TABLE_LOCK = _threading_mod.Lock()


class FakeJobsTable:
    """Implements the surface JobStore needs (see syteline.jobs.DynamoTable)."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["pk"])
        return {"Item": dict(item)} if item else {}

    def transact_put(self, puts):
        for item, cond in puts:
            if cond and item["pk"] in self.items:
                raise FakeClientError("TransactionCanceledException")
        for item, _ in puts:
            self.items[item["pk"]] = dict(item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ConditionExpression=None,
                    ExpressionAttributeNames=None):
        item = self.items[Key["pk"]]
        names = ExpressionAttributeNames or {}
        vals = ExpressionAttributeValues
        if ConditionExpression and item["state"] != vals[":old"]:
            raise FakeClientError("ConditionalCheckFailedException")
        for a in UpdateExpression[len("SET "):].split(", "):
            lhs, rhs = (x.strip() for x in a.split("=", 1))
            if rhs.startswith(lhs + " +"):  # attempts = attempts + :one
                item[lhs] = item.get(lhs, 0) + vals[":one"]
            else:
                item[names.get(lhs, lhs)] = vals[rhs]

    def delete_item(self, Key):
        self.items.pop(Key["pk"], None)

    # conditional put / delete of the ERP-write lock (specs/007): the two conditions the code uses, evaluated atomically

    def put_if(self, item, condition, values):
        with self._guard():
            current = self.items.get(item["pk"])
            if condition == "attribute_not_exists(pk) OR expires_at <= :now":
                ok = current is None or int(current["expires_at"]) <= int(values[":now"])
            else:
                raise AssertionError(f"unsupported condition {condition}")
            if not ok:
                raise FakeClientError("ConditionalCheckFailedException")
            self.items[item["pk"]] = dict(item)

    def delete_if(self, Key, condition, values):
        with self._guard():
            current = self.items.get(Key["pk"])
            if condition != "holder = :h":
                raise AssertionError(f"unsupported condition {condition}")
            if current is None or current.get("holder") != values[":h"]:
                raise FakeClientError("ConditionalCheckFailedException")
            del self.items[Key["pk"]]

    @staticmethod
    def _guard():
        return _FAKE_TABLE_LOCK

    def scan_all(self):
        return [dict(i) for i in self.items.values()]


class FakeLambda:
    """Records async invokes; can be told to fail."""

    def __init__(self, fail=False):
        self.invocations = []
        self.fail = fail

    def invoke(self, FunctionName, InvocationType, Payload):
        if self.fail:
            raise RuntimeError("invoke failed")
        self.invocations.append((FunctionName, InvocationType, Payload))
        return {"StatusCode": 202}


class FakeIdp:
    """Stands in for idp_sdk.IDPClient: the document / assessment surface that syteline.idp uses.

    add(document_id, fields, status="COMPLETED", document_class=None, low=(...)) registers a document; `low` lists
    result names (top-level names or full paths) whose assessed confidence is below its threshold.
    raise_for: {document_id: exception} makes any read of that document raise it; `calls` records every read.
    """

    class _Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def __init__(self):
        self.docs = {}
        self.raise_for = {}
        self.calls = []
        self.document = self
        self.assessment = self

    def add(self, document_id, fields, status="COMPLETED", document_class=None, low=(), pages=1, issues=0):
        self.docs[document_id] = {"fields": fields, "status": status, "class": document_class, "low": set(low),
                                  "pages": pages, "issues": issues}
        return self

    def update(self, document_id, **changes):
        self.docs[document_id].update(changes)

    def _doc(self, document_id, op):
        self.calls.append((op, document_id))
        if document_id in self.raise_for:
            raise self.raise_for[document_id]
        if document_id not in self.docs:
            err = type("IDPResourceNotFoundError", (Exception,), {})
            raise err(f"Results not found for document: {document_id}")
        return self.docs[document_id]

    # document.*
    def get_status(self, document_id, **kw):
        d = self._doc(document_id, "status")
        return self._Obj(document_id=document_id, status=self._Obj(value=d["status"]), num_pages=d.get("pages"),
                         processing_issue_count=d.get("issues"))

    def get_metadata(self, document_id, section_id=1, **kw):
        d = self._doc(document_id, "metadata")
        return self._Obj(document_id=document_id, document_class=d["class"], fields=d["fields"], confidence=None)

    # assessment.*
    def get_confidence(self, document_id, section_id=1, **kw):
        d = self._doc(document_id, "confidence")
        attrs = {}
        for name in set(_flat_names(d["fields"])) | d["low"]:
            attrs[name] = self._Obj(confidence=0.4 if name in d["low"] else 0.97,
                                    meets_threshold=name not in d["low"])
        return self._Obj(document_id=document_id, section_id=section_id, attributes=attrs)


def _flat_names(fields):
    return [k for k in fields]


def po_fields(n=2, **over):
    """An IDP result `fields` dict in the default (UNCONFIRMED) mapping of syteline.idp_fields."""
    f = {"customer_name": "ACME", "cust_po": "PO-1",
         "lines": [{"part_number": chr(ord("A") + i), "quantity": i + 1, "uom": "EA"} for i in range(n)]}
    f.update(over)
    return f


# ---- a fake SQL Server (DB-API shaped), for the database calls of feature 005 --------------------------
import threading as _threading  # noqa: E402

_SQL_ERR = {n: type(n, (Exception,), {}) for n in ("LoginError", "DatabaseError", "ClosedConnectionError",
                                                    "TimeoutError", "InterfaceError")}
LoginError, DatabaseError, ClosedConnectionError, SqlTimeoutError, InterfaceError = (
    _SQL_ERR["LoginError"], _SQL_ERR["DatabaseError"], _SQL_ERR["ClosedConnectionError"], _SQL_ERR["TimeoutError"],
    _SQL_ERR["InterfaceError"])

DB_CFG = {"SQL_SERVER": "sql.plant.example", "SQL_DATABASE": "SL_PROD", "SQL_USER": "svc-login",
          "SQL_PASS": "S3cr3t-DB-Pass", "SQL_PORT": "1433",
          "SQL_CA_PEM": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"}


class FakeSqlServer:
    """Records every statement; serves the two numbering routines and the two SELECTs of syteline.sqlserver.

    scenarios: {("batch", nth) | ("commit", nth) | ("select", nth): action}, nth counted per kind from 1.
      batch:  "error" (routine raises a database error), "drop" (connection lost after the batch), "null" (NULL key),
              "bad" (unreadable key), "empty" (no row)
      commit: "drop" (connection lost during the commit)
      select: "error" (database error), "drop" (connection lost)
    connect_error: None | "connect_fail" | "login_fail".  key_format: "{n}", "^{n}^", "^{n},{m}^" ({m} = n + 1).
    custaddr: list of dicts with cust_num, cust_seq, name, addr1..addr4, city, state, zip, country.
    co_rows: list of dicts with co_num, cust_num, cust_seq, terms_code, ship_code.
    """

    def __init__(self, co_start=100, job_start=5000, key_format="{n}", custaddr=None, co_rows=None, scenarios=None,
                 connect_error=None, infobar=""):
        self._lock = _threading.Lock()
        self.counters = {"NextCoSp": co_start, "NextWOSp": job_start}
        self.key_format, self.infobar = key_format, infobar
        self.custaddr, self.co_rows = list(custaddr or []), list(co_rows or [])
        self.scenarios, self.connect_error = dict(scenarios or {}), connect_error
        self.statements = []      # (sql, params) in order, across all connections
        self.connect_kwargs = []  # keyword arguments of every connect call
        self.opened = self.closed = 0
        self.committed, self.rolled_back = [], 0
        self._n = {"batch": 0, "commit": 0, "select": 0}

    def install(self, monkeypatch):
        from syteline import sqlserver

        monkeypatch.setattr(sqlserver, "_connect", self.connect)
        return self

    def connect(self, **kw):
        self.connect_kwargs.append(kw)
        if self.connect_error == "connect_fail":
            raise OSError("connection refused")
        if self.connect_error == "login_fail":
            raise LoginError(f"Login failed for user '{kw.get('user')}' password '{kw.get('password')}'")
        with self._lock:
            self.opened += 1
        return _FakeSqlConnection(self)

    def _scenario(self, kind):
        with self._lock:
            self._n[kind] += 1
            return self.scenarios.get((kind, self._n[kind]))

    def statements_text(self):
        return [s for s, _ in self.statements]


class _FakeSqlConnection:
    def __init__(self, server):
        self._s, self._open = server, True

    def cursor(self):
        return _FakeSqlCursor(self._s)

    def close(self):
        if self._open:
            self._open = False
            with self._s._lock:
                self._s.closed += 1


class _FakeSqlCursor:
    def __init__(self, server):
        self._s, self.description, self._rows = server, None, []

    def close(self):
        pass

    def nextset(self):
        return False

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def execute(self, sql, params=()):
        import re

        s = self._s
        with s._lock:
            s.statements.append((sql, tuple(params)))
        self.description, self._rows = None, []
        if sql.startswith("SET NOCOUNT ON"):
            routine = re.search(r"EXEC \[dbo\]\.\[(\w+)\]", sql).group(1)
            action = s._scenario("batch")
            if action == "error":
                raise DatabaseError(f"{routine} failed: site not found")
            if action == "drop":
                raise ClosedConnectionError("connection closed by the server")
            with s._lock:
                n = s.counters[routine]
                s.counters[routine] += 1
            self.description = [("key_val",), ("infobar",)]
            if action == "empty":
                return
            key = {"null": None, "bad": "abc"}.get(action, s.key_format.format(n=n, m=n + 1))
            self._rows = [(key, s.infobar)]
            s._last = n
            return
        if sql == "COMMIT TRANSACTION":
            if s._scenario("commit") == "drop":
                raise ClosedConnectionError("connection lost during commit")
            with s._lock:
                s.committed.append(getattr(s, "_last", None))
            return
        if sql.startswith("IF @@TRANCOUNT"):
            with s._lock:
                s.rolled_back += 1
            return
        action = s._scenario("select")
        if action == "error":
            raise DatabaseError("Invalid object name")
        if action == "drop":
            raise ClosedConnectionError("connection lost")
        if sql == "SELECT 1":
            self.description, self._rows = [("",)], [(1,)]
        elif "FROM custaddr" in sql:
            clauses = re.findall(r"(LOWER\(name\) LIKE %s|zip LIKE %s)", sql)
            hit = []
            for row in s.custaddr:
                for clause, p in zip(clauses, params):
                    if clause.startswith("LOWER"):
                        ok = p.strip("%") in str(row.get("name", "")).lower()
                    else:
                        ok = str(row.get("zip", "")).startswith(p.rstrip("%"))
                    if ok:
                        hit.append(row)
                        break
            cols = ["cust_num", "cust_seq", "name", "addr1", "addr2", "addr3", "addr4", "city", "state", "zip",
                    "country"]
            cap = int(re.search(r"SELECT TOP (\d+)", sql).group(1))
            hit.sort(key=lambda r: (str(r["cust_num"]), r["cust_seq"]))
            self.description = [(c,) for c in cols]
            self._rows = [tuple(r.get(c, "") for c in cols) for r in hit[:cap]]
        elif "FROM co " in sql or "FROM co\n" in sql or "FROM co WHERE" in sql:
            cust = params[0]
            rows = [r for r in s.co_rows if str(r["cust_num"]).strip() == cust]
            if len(params) > 1:
                rows = [r for r in rows if r["cust_seq"] == params[1]]
            rows.sort(key=lambda r: r["co_num"], reverse=True)
            self.description = [("terms_code",), ("ship_code",)]
            self._rows = [(r["terms_code"], r["ship_code"]) for r in rows[:1]]


# ---- the processing status change event of feature 006 -----------------------------------------------------
def status_change_event(document_id="159112.pdf", status="SUCCEEDED", source="aws.states",
                        detail_type="Step Functions Execution Status Change", input_id=Ellipsis, output_id=Ellipsis,
                        time="2026-10-22T23:02:29Z", name=None, as_text=False, extra_input=None):
    """The EventBridge event the IDP state machine emits. `input_id` / `output_id` default to `document_id`; None leaves
    the document out of that text, a string is used as the raw text (to test invalid JSON)."""
    import json

    def text(doc_id, body):
        if doc_id is Ellipsis:
            doc_id = document_id
        if isinstance(doc_id, str) and doc_id.startswith("RAW:"):
            return doc_id[4:]
        doc = {} if doc_id is None else {"document": {"id": doc_id, **(body or {})}}
        return json.dumps(doc)

    event = {"version": "0", "id": "40f0b36a-31bb-2911-0414-3e99f8f8b4b4", "detail-type": detail_type, "source": source,
             "account": "108375521309", "time": time, "region": "us-gov-east-1",
             "detail": {"executionArn": "arn:aws-us-gov:states:us-gov-east-1:108375521309:execution:wf:"
                                        + (name or f"{document_id}-33b430ca"),
                        "name": name or f"{document_id}-33b430ca", "status": status,
                        "input": text(input_id, extra_input), "output": text(output_id, {"status": "EVALUATING"})}}
    return json.dumps(event) if as_text else event

