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
        return [c for c in self.calls if c[0] in ("insert", "invoke")]


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


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
