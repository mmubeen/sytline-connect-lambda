"""In-memory fakes: SOAP IDO client, SQL client, DynamoDB job table, Lambda invoker."""

import itertools

from syteline import config
from syteline.errors import ErpRejected, ErpUncertain, ErpUnreachable, LiveWritesDisabled


class FakeSoap:
    """Implements the SoapIdoClient surface (load / insert / read_back). There is NO delete.

    scenarios: {(op, ido, nth): action} where action is
      "reject"            -> ErpRejected, row NOT stored (row-level rejection inside an HTTP 200)
      "timeout_after"     -> row stored, then ErpUncertain (timeout after the insert was accepted)
      "timeout_before"    -> row NOT stored, ErpUncertain
      "unreachable"       -> ErpUnreachable (definitely not sent)
    nth is the 1-based count of inserts into that IDO. read_fail makes load raise ErpUnreachable.
    corrupt: {(ido, prop): value} alters what read-back returns (read-back mismatch).
    """

    def __init__(self, scenarios=None, read_fail=False, corrupt=None, enforce_live_gate=True):
        self.rows = {}
        self.scenarios = dict(scenarios or {})
        self.read_fail = read_fail
        self.corrupt = dict(corrupt or {})
        self.calls = []
        self._count = {}
        self._rp = itertools.count(1)
        self._gate = enforce_live_gate

    def insert(self, ido, values):
        self.calls.append(("insert", ido))
        if self._gate and not config.live_writes_enabled():
            raise LiveWritesDisabled("live writes disabled")
        n = self._count[ido] = self._count.get(ido, 0) + 1
        action = self.scenarios.get(("insert", ido, n))
        if action == "reject":
            raise ErpRejected(f"{ido} row rejected by ERP")
        if action == "timeout_before":
            raise ErpUncertain("timeout")
        if action == "unreachable":
            raise ErpUnreachable("connect failed")
        row = dict(values, RowPointer=f"rp-{next(self._rp)}")
        self.rows.setdefault(ido, []).append(row)
        if action == "timeout_after":
            raise ErpUncertain("timeout after insert")
        return row

    def load(self, ido, properties, filter_=""):
        self.calls.append(("load", ido))
        if self.read_fail:
            raise ErpUnreachable("read-back failed")
        rows = [dict(r) for r in self.rows.get(ido, [])]
        if "=" in filter_:
            key, val = filter_.split("=", 1)
            key, val = key.strip(), val.strip().strip("'")
            rows = [r for r in rows if str(r.get(key)) == val]
        for (c_ido, prop), value in self.corrupt.items():
            if c_ido == ido:
                for r in rows:
                    r[prop] = value
        return rows

    read_back = load

    def count(self, ido):
        return len(self.rows.get(ido, []))


class FakeSql:
    """Implements SqlClient.query / execute_write for the statements the code uses."""

    def __init__(self, customers=None, records=None, notes=None, connect_fail=False):
        self.customers = list(customers or [])  # rows from custaddr
        self.records = dict(records or {})  # {("co","100"): "rowpointer"}
        self.notes = list(notes or [])  # dicts as returned by the notes query
        self.allocations = []
        self.writes = []
        self.queries = []
        self.connect_fail = connect_fail
        self._co = itertools.count(100)
        self._job = itertools.count(5000)

    def query(self, sql, params=()):
        import re

        self.queries.append((sql, tuple(params)))
        if self.connect_fail:
            raise ErpUnreachable("SQL connection failed")
        low = sql.lower()
        if "from custaddr" in low:
            cols = re.findall(r"UPPER\(LTRIM\(RTRIM\(([^)]+)\)\)\) = %s", sql)
            out = []
            for c in self.customers:
                if all(str(c.get(col.strip("[]"), "")).strip().upper() == p for col, p in zip(cols, params)):
                    out.append({"cust_num": c["cust_num"], "cust_seq": c.get("cust_seq", 0)})
            return out
        if "from specificnotes" in low:
            rp = params[0]
            rows = [dict(n) for n in self.notes if n["RefRowPointer"] == rp]
            if len(params) > 1:
                rows = [r for r in rows if r["CreateDate"] >= params[1]]
            return rows
        m = re.search(r"select rowpointer from (\w+) where", low)
        if m:
            for (t, k), rp in self.records.items():
                if t == m.group(1) and k == params[0]:
                    return [{"RowPointer": rp}]
            return []
        raise AssertionError(f"unexpected query: {sql}")

    def execute_write(self, sql, params=()):
        if not config.live_writes_enabled():
            raise LiveWritesDisabled("live writes disabled")
        low = sql.lower()
        self.writes.append((sql, tuple(params)))
        if "nextcosp" in low:
            self.allocations.append("co")
            return [{"Key": str(next(self._co)), "Infobar": None}]
        if "nextwosp" in low:
            self.allocations.append("job")
            return [{"Key": str(next(self._job)), "Infobar": None}]
        if "createspecificnotesp" in low:
            obj, rp, desc, text, internal = params
            self.notes.append({"SpecificNoteToken": len(self.notes) + 1, "RefRowPointer": rp,
                               "NoteContent": text, "NoteDesc": desc, "CreateDate": "2026-10-20",
                               "CreatedBy": "svc", "NhNoteFlag": internal})
            return []
        raise AssertionError(f"unexpected write: {sql}")


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
