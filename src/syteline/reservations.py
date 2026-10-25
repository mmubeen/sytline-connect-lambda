"""DynamoDB job-number reservation store and state machine (see specs/004-job-number-workflow/data-model.md).

States: reserving -> reserved -> save_accepted -> saving -> saved | partly_saved | needs_reconciliation,
and reserving -> failed. Nothing here ever moves a reservation out of needs_reconciliation, and nothing
replays a save; only `save_accepted` reservations (where no write can have happened) are listed for re-driving.
Items live in the same table as the upload jobs, under the keys RESERVATION#<id> and RESREF#<request_ref>.
"""

import os
import time
import uuid

from .jobs import TTL_AFTER_TERMINAL, DynamoTable, RefConflict, _cond_failed

RESERVING, RESERVED, SAVE_ACCEPTED, SAVING, SAVED, PARTLY_SAVED, FAILED, NEEDS_RECONCILIATION = (
    "reserving", "reserved", "save_accepted", "saving", "saved", "partly_saved", "failed", "needs_reconciliation")
TERMINAL = (SAVED, PARTLY_SAVED, FAILED, NEEDS_RECONCILIATION)

PK = "RESERVATION#"
REF_PK = "RESREF#"
DOC_PK = "DOCRES#"  # document ID -> its latest reservation (specs/006: an event must see an API-made reservation)


class ReservationStore:
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
    def create_reservation(self, request, payload_hash, fingerprint, uploaded_by, caller_identity):
        """Create the reservation (state `reserving`) and its request_ref index atomically.

        `request` carries request_ref, document_id and line_count (counted from the IDP document); the PO details
        (customer, PO number, lines) are never stored.

        Returns (reservation, duplicate). Same ref + same hash -> (existing reservation, True).
        Same ref + different hash -> RefConflict.
        """
        ref = request["request_ref"]
        now = self._now()
        rid = str(uuid.uuid4())
        item = {"pk": f"{PK}{rid}", "reservation_id": rid, "request_ref": ref, "payload_hash": payload_hash,
                "fingerprint": fingerprint, "document_id": request["document_id"], "line_count": request["line_count"],
                "uploaded_by": uploaded_by, "caller_identity": caller_identity, "state": RESERVING,
                "job_numbers": [], "attempts": 0, "created_at": now, "updated_at": now}
        index = {"pk": f"{REF_PK}{ref}", "request_ref": ref, "reservation_id": rid, "payload_hash": payload_hash}
        marker = {"pk": f"{DOC_PK}{request['document_id']}", "document_id": request["document_id"],
                  "reservation_id": rid, "created_at": now}
        try:
            self._t.transact_put([(item, None), (index, "attribute_not_exists(pk)"), (marker, None)])
            return item, False
        except Exception as exc:
            if not _cond_failed(exc):
                raise
        existing = self._t.get_item(Key={"pk": f"{REF_PK}{ref}"}).get("Item")
        if existing is None:
            raise RuntimeError("request index vanished during create")
        if existing["payload_hash"] != payload_hash:
            raise RefConflict(ref)
        return self.get(existing["reservation_id"]), True

    def get(self, reservation_id):
        return self._t.get_item(Key={"pk": f"{PK}{reservation_id}"}).get("Item")

    @property
    def table(self):
        return self._t

    def reservation_for_document(self, document_id):
        """The latest reservation made for the IDP document (by either workflow), or None."""
        marker = self._t.get_item(Key={"pk": f"{DOC_PK}{document_id}"}).get("Item")
        return self.get(marker["reservation_id"]) if marker else None

    # -- transitions (all conditional on the expected state) ----------------------------
    def _transition(self, reservation_id, expected, new_state, extra=None):
        names = {"#s": "state"}
        vals = {":new": new_state, ":old": expected, ":now": self._now()}
        expr = ["#s = :new", "updated_at = :now"]
        for k, v in (extra or {}).items():
            names[f"#{k}"] = k
            vals[f":{k}"] = v
            expr.append(f"#{k} = :{k}")
        if new_state in TERMINAL:  # ttl only once the reservation is finished
            vals[":ttl"] = self._now() + TTL_AFTER_TERMINAL
            names["#ttl"] = "ttl"  # TTL is a DynamoDB reserved word: it cannot appear in an expression unaliased
            expr.append("#ttl = :ttl")
        try:
            self._t.update_item(Key={"pk": f"{PK}{reservation_id}"}, UpdateExpression="SET " + ", ".join(expr),
                                ConditionExpression="#s = :old", ExpressionAttributeNames=names,
                                ExpressionAttributeValues=vals)
            return True
        except Exception as exc:
            if _cond_failed(exc):
                return False
            raise

    # reserving
    def set_customer(self, reservation_id, customer_match):
        return self._transition(reservation_id, RESERVING, RESERVING, {"customer_match": customer_match})

    def set_co_num(self, reservation_id, co_num):
        return self._transition(reservation_id, RESERVING, RESERVING, {"co_num": co_num})

    def add_job_number(self, reservation_id, job_numbers):
        """Persist the whole list so far (the reserving run is the only writer, so no list-append is needed)."""
        return self._transition(reservation_id, RESERVING, RESERVING, {"job_numbers": job_numbers})

    def mark_reserved(self, reservation_id):
        return self._transition(reservation_id, RESERVING, RESERVED)

    def mark_failed(self, reservation_id, code, message, step, expected=RESERVING):
        return self._transition(reservation_id, expected, FAILED,
                                {"failure": {"code": code, "message": message, "step": step}})

    # save
    def accept_save(self, reservation_id, params):
        """reserved -> save_accepted. False if the reservation is not `reserved` (a reservation is used once).

        `params` is the document ID and the few optional non-PO values of the request (order_date, received_date,
        warehouse, order_type); the PO details are read from IDP again when the save runs.
        """
        return self._transition(reservation_id, RESERVED, SAVE_ACCEPTED, {
            "save": dict(params, requested_at=self._now())})

    def claim(self, reservation_id):
        """save_accepted -> saving. False if not save_accepted (never re-run a save)."""
        ok = self._transition(reservation_id, SAVE_ACCEPTED, SAVING)
        if ok:
            self._t.update_item(Key={"pk": f"{PK}{reservation_id}"},
                                UpdateExpression="SET attempts = attempts + :one",
                                ExpressionAttributeValues={":one": 1})
        return ok

    def release_claim(self, reservation_id):
        """saving -> save_accepted, only for a run that wrote nothing (the IDP source was unavailable): the sweeper
        starts it again later."""
        return self._transition(reservation_id, SAVING, SAVE_ACCEPTED)

    def record_progress(self, reservation_id, possibly_written, results=None):
        """Persist what may exist in the ERP so far (used if the worker dies mid-save)."""
        return self._transition(reservation_id, SAVING, SAVING, {
            "erp_records": {"possibly_written": possibly_written}, "results": results or []})

    def finish_saved(self, reservation_id, results, warnings=None):
        return self._transition(reservation_id, SAVING, SAVED, {"results": results, "warnings": list(warnings or [])})

    def finish_partly_saved(self, reservation_id, code, message, step, results, warnings=None):
        return self._transition(reservation_id, SAVING, PARTLY_SAVED, {
            "failure": {"code": code, "message": message, "step": step}, "results": results,
            "warnings": list(warnings or [])})

    def park(self, reservation_id, code, message, step, possibly_written, results=None, expected=SAVING,
             warnings=None):
        return self._transition(reservation_id, expected, NEEDS_RECONCILIATION, {
            "failure": {"code": code, "message": message, "step": step},
            "erp_records": {"possibly_written": possibly_written}, "results": results or [],
            "warnings": list(warnings or [])})

    def fail_save(self, reservation_id, code, message, step, results=None, warnings=None):
        """A save that ended before anything was written."""
        return self._transition(reservation_id, SAVING, FAILED, {
            "failure": {"code": code, "message": message, "step": step}, "results": results or [],
            "warnings": list(warnings or [])})

    # -- sweeper queries -----------------------------------------------------------------
    def _scan_state(self, state, older_than_seconds):
        cutoff = self._now() - older_than_seconds
        return [i for i in self._t.scan_all() if i.get("state") == state and i["updated_at"] <= cutoff
                and i["pk"].startswith(PK)]

    def list_save_accepted_older_than(self, seconds):
        return self._scan_state(SAVE_ACCEPTED, seconds)

    def list_saving_older_than(self, seconds):
        return self._scan_state(SAVING, seconds)


# -- reporting ---------------------------------------------------------------------------
RESERVE_STALE_SECONDS = 60  # a `reserving` item older than the reserve function's timeout never finished


def effective_state(item, now):
    """The stored state, except that a reservation stuck in `reserving` past the function timeout reads as failed."""
    if item["state"] == RESERVING and now - int(item["updated_at"]) > RESERVE_STALE_SECONDS:
        return FAILED
    return item["state"]


def numbers_report(item, now):
    """{"co_num", "jobs": [{"line", "job", "usage"}]} where usage says what became of each reserved number.

    usage: `reserved` (waiting for its save), `used` (line saved and verified), `consumed_unused` (reservation or
    line failed: the number is gone, no record was written for it), `unverified` (the save needs reconciliation
    and this line was not confirmed saved: a record may exist, check SyteLine), `supplied_not_saved` (as
    `consumed_unused`, but for a job number the document supplied: it was never allocated by this service).
    """
    state = effective_state(item, now)
    results = {int(r["line"]): r for r in item.get("results") or []}
    jobs = []
    for entry in item.get("job_numbers") or []:
        line = int(entry["line"])
        if state == FAILED:
            usage = "consumed_unused"
        elif state in (RESERVED, SAVE_ACCEPTED, SAVING, RESERVING):
            usage = "reserved"
        elif results.get(line, {}).get("status") == "saved":
            usage = "used"
        elif state == NEEDS_RECONCILIATION:
            usage = "unverified"
        else:  # partly_saved: the failed line's rows were proven absent, later lines were never attempted
            usage = "consumed_unused"
        source = entry.get("source", "allocated")  # entries stored before the optional job number have none
        if source == "document" and usage == "consumed_unused":
            usage = "supplied_not_saved"  # this service did not consume a number the document brought
        jobs.append({"line": line, "job": str(entry["job"]), "source": source, "usage": usage})
    return {"co_num": item.get("co_num"), "jobs": jobs}
