"""DynamoDB upload-job store and state machine (see data-model.md).

States: accepted -> running -> succeeded | failed | needs_reconciliation.
Nothing here ever moves a job out of needs_reconciliation, and nothing replays an upload; only
`accepted` jobs (where no write can have happened) are listed for re-driving.
"""

import os
import time
import uuid

ACCEPTED, RUNNING, SUCCEEDED, FAILED, NEEDS_RECONCILIATION = (
    "accepted", "running", "succeeded", "failed", "needs_reconciliation")
TERMINAL = (SUCCEEDED, FAILED, NEEDS_RECONCILIATION)
TTL_AFTER_TERMINAL = 90 * 24 * 3600


def _cond_failed(exc):
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return code in ("ConditionalCheckFailedException", "TransactionCanceledException")


class RefConflict(Exception):
    """request_ref already used with a different payload."""


class DynamoTable:
    """Thin adapter over a boto3 DynamoDB Table resource; the surface JobStore needs (fakeable)."""

    def __init__(self, table):
        self._table = table

    def get_item(self, Key):
        return self._table.get_item(Key=Key, ConsistentRead=True)

    def update_item(self, **kw):
        return self._table.update_item(**kw)

    def delete_item(self, Key):
        return self._table.delete_item(Key=Key)

    def transact_put(self, puts):
        """All-or-nothing puts: list of (item, condition_expression_or_None)."""
        items = []
        for item, cond in puts:
            put = {"TableName": self._table.name, "Item": item}
            if cond:
                put["ConditionExpression"] = cond
            items.append({"Put": put})
        self._table.meta.client.transact_write_items(TransactItems=items)

    def scan_all(self):
        out, kw = [], {"ConsistentRead": True}
        while True:
            resp = self._table.scan(**kw)
            out.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return out
            kw["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


class JobStore:
    def __init__(self, table, clock=time.time):
        self._t = table
        self._clock = clock

    @classmethod
    def from_env(cls):
        import boto3

        return cls(DynamoTable(boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])))

    def _now(self):
        return int(self._clock())

    # -- create / read -----------------------------------------------------------------
    def create_job(self, request, payload_hash, uploaded_by, caller_identity):
        """Create the job and its request_ref index atomically.

        Returns (job, duplicate). Same ref + same hash -> (existing job, True).
        Same ref + different hash -> RefConflict.
        """
        ref = request["request_ref"]
        now = self._now()
        upload_id = str(uuid.uuid4())
        job = {"pk": f"UPLOAD#{upload_id}", "upload_id": upload_id, "request_ref": ref, "payload_hash": payload_hash,
               "request": request, "uploaded_by": uploaded_by, "caller_identity": caller_identity,
               "state": ACCEPTED, "attempts": 0, "created_at": now, "updated_at": now}
        index = {"pk": f"REF#{ref}", "request_ref": ref, "upload_id": upload_id, "payload_hash": payload_hash}
        try:
            self._t.transact_put([(job, None), (index, "attribute_not_exists(pk)")])
            return job, False
        except Exception as exc:
            if not _cond_failed(exc):
                raise
        existing_index = self._t.get_item(Key={"pk": f"REF#{ref}"}).get("Item")
        if existing_index is None:
            raise RuntimeError("request index vanished during create")
        if existing_index["payload_hash"] != payload_hash:
            raise RefConflict(ref)
        return self.get_job(existing_index["upload_id"]), True

    def get_job(self, upload_id):
        return self._t.get_item(Key={"pk": f"UPLOAD#{upload_id}"}).get("Item")

    # -- transitions (all conditional) ---------------------------------------------------
    def _transition(self, upload_id, expected, new_state, extra=None):
        names = {"#s": "state"}
        vals = {":new": new_state, ":old": expected, ":now": self._now()}
        expr = ["#s = :new", "updated_at = :now"]
        for k, v in (extra or {}).items():
            names[f"#{k}"] = k
            vals[f":{k}"] = v
            expr.append(f"#{k} = :{k}")
        if new_state in TERMINAL:  # ttl only once the job is finished
            vals[":ttl"] = self._now() + TTL_AFTER_TERMINAL
            expr.append("ttl = :ttl")
        try:
            self._t.update_item(Key={"pk": f"UPLOAD#{upload_id}"}, UpdateExpression="SET " + ", ".join(expr),
                                ConditionExpression="#s = :old", ExpressionAttributeNames=names,
                                ExpressionAttributeValues=vals)
            return True
        except Exception as exc:
            if _cond_failed(exc):
                return False
            raise

    def claim(self, upload_id):
        """accepted -> running. False if the job is not accepted (never re-run an upload)."""
        ok = self._transition(upload_id, ACCEPTED, RUNNING)
        if ok:
            self._t.update_item(Key={"pk": f"UPLOAD#{upload_id}"}, UpdateExpression="SET attempts = attempts + :one",
                                ExpressionAttributeValues={":one": 1})
        return ok

    def record_progress(self, upload_id, possibly_written, customer_match=None):
        """Persist what may exist in the ERP so far (used if the worker dies mid-upload)."""
        return self._transition(upload_id, RUNNING, RUNNING, {
            "erp_records": {"possibly_written": possibly_written},
            "customer_match": customer_match or {}})

    def succeed(self, upload_id, co_num, lines, customer_match=None):
        return self._transition(upload_id, RUNNING, SUCCEEDED, {
            "erp_records": {"co_num": co_num, "lines": lines},
            "customer_match": customer_match or {}})

    def fail(self, upload_id, code, message, step, customer_match=None):
        return self._transition(upload_id, RUNNING, FAILED, {
            "failure": {"code": code, "message": message, "step": step},
            "customer_match": customer_match or {}})

    def park(self, upload_id, code, message, step, possibly_written, expected=RUNNING, customer_match=None):
        return self._transition(upload_id, expected, NEEDS_RECONCILIATION, {
            "failure": {"code": code, "message": message, "step": step},
            "erp_records": {"possibly_written": possibly_written},
            "customer_match": customer_match or {}})

    # -- sweeper queries -----------------------------------------------------------------
    def _scan_state(self, state, older_than_seconds):
        cutoff = self._now() - older_than_seconds
        return [i for i in self._t.scan_all() if i.get("state") == state and i["updated_at"] <= cutoff
                and i["pk"].startswith("UPLOAD#")]

    def list_accepted_older_than(self, seconds):
        return self._scan_state(ACCEPTED, seconds)

    def list_running_older_than(self, seconds):
        return self._scan_state(RUNNING, seconds)
