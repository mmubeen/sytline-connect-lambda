"""The ERP-write lease lock (specs/007, research R6, data-model "LOCK#erp-writes").

With one function there is no reserved concurrency of one to keep ERP writes apart, so the background saves and uploads
take this lock in the jobs table before they claim a job. The lease is the function timeout plus 30 seconds: a holder
that died blocks others for at most that long. Only the holder releases it. No PO data is stored.
"""

import os
import time

from .jobs import DynamoTable, _cond_failed

PK = "LOCK#erp-writes"
LEASE_SECONDS = 330  # the function timeout (300) + 30
TTL_AFTER_EXPIRY = 3600
POLL_SECONDS = 2
ACQUIRE = "attribute_not_exists(pk) OR expires_at <= :now"
RELEASE = "holder = :h"


class ErpLock:
    def __init__(self, table, clock=time.time, sleep=time.sleep):
        self._t, self._clock, self._sleep = table, clock, sleep

    @classmethod
    def from_env(cls):
        import boto3

        return cls(DynamoTable(boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])))

    def _try(self, holder, action):
        now = int(self._clock())
        item = {"pk": PK, "holder": holder[:128], "action": action, "acquired_at": now,
                "expires_at": now + LEASE_SECONDS, "ttl": now + LEASE_SECONDS + TTL_AFTER_EXPIRY}
        try:
            self._t.put_if(item, ACQUIRE, {":now": now})
            return True
        except Exception as exc:
            if _cond_failed(exc):
                return False
            raise

    def acquire(self, holder, action, wait_seconds, poll=POLL_SECONDS):
        """True once this holder owns the lock; False if it was still held when `wait_seconds` ran out."""
        deadline = self._clock() + max(0, wait_seconds)
        while True:
            if self._try(holder, action):
                return True
            if self._clock() + poll > deadline:
                return False
            self._sleep(poll)

    def still_holder(self, holder):
        item = self._t.get_item(Key={"pk": PK}).get("Item")
        return bool(item) and item.get("holder") == holder[:128] and int(item["expires_at"]) > int(self._clock())

    def release(self, holder):
        """Delete the lock only if this holder still owns it (an expired, taken-over lock is not ours to delete)."""
        try:
            self._t.delete_if({"pk": PK}, RELEASE, {":h": holder[:128]})
        except Exception as exc:
            if not _cond_failed(exc):
                raise
