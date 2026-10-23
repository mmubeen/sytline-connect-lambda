"""Once per document, and the outcome of an event run (specs/006, research R5, data-model "EVTDOC#").

An `EVTDOC#<document_id>` item is created by a conditional put: the first delivery of an event for a document claims it,
every other delivery (a repeat, a concurrent one) finds the claim and returns what it recorded. A run that reserved no
number releases the claim so a later delivery can proceed. Only identifiers, states and outcome codes are stored; never
a field value or any PO detail.
"""

import time

from .jobs import TTL_AFTER_TERMINAL, _cond_failed

PK = "EVTDOC#"
PROCESSING = "processing"
STALE_AFTER = 300  # seconds: a claim older than this with no reservation is from a run that died (function timeout is 60)
FINAL_STATES = ("save_accepted", "finished")


class EventStore:
    def __init__(self, table, clock=time.time):
        self._t = table
        self._clock = clock

    def _now(self):
        return int(self._clock())

    def get(self, document_id):
        return self._t.get_item(Key={"pk": f"{PK}{document_id}"}).get("Item")

    def claim(self, document_id, execution, _retry=True):
        """True for the delivery that now owns the document; False if it was already claimed."""
        now = self._now()
        item = {"pk": f"{PK}{document_id}", "document_id": document_id, "execution": execution, "state": PROCESSING,
                "claimed_at": now, "updated_at": now}
        try:
            self._t.transact_put([(item, "attribute_not_exists(pk)")])
            return True
        except Exception as exc:
            if not _cond_failed(exc):
                raise
        current = self.get(document_id)
        if (_retry and current and current.get("state") == PROCESSING and not current.get("reservation_id")
                and now - int(current.get("claimed_at", now)) > STALE_AFTER):
            self.release(document_id)  # the run that claimed it died before reserving anything
            return self.claim(document_id, execution, _retry=False)
        return False

    def release(self, document_id):
        """Give the claim back: nothing was reserved, so a later delivery may try again."""
        self._t.delete_item(Key={"pk": f"{PK}{document_id}"})

    def record(self, document_id, outcome, reservation_id=None, state=None):
        item = self.get(document_id)
        if item is None:
            return
        item.update(outcome=outcome, updated_at=self._now())
        if reservation_id:
            item["reservation_id"] = reservation_id
        if state:
            item["state"] = state
        if item["state"] in FINAL_STATES:
            item["ttl"] = self._now() + TTL_AFTER_TERMINAL
        self._t.transact_put([(item, None)])
