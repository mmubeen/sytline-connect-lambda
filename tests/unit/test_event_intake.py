"""The event intake (specs/006): read the document named in the processing-complete event, reserve and save once."""

import json
import logging
import os

import pytest

from fakes import FakeIdp, FakeJobsTable, FakeLambda, FakeSoap, po_fields, status_change_event as ev
from handlers import event_intake as ei
from syteline import errors, eventstore, idp
from syteline.reservations import ReservationStore

DOC = "159112.pdf"
CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]
SAMPLE = json.load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "sample_event.json")))


class World:
    def __init__(self, monkeypatch):
        self.idp = FakeIdp().add(DOC, po_fields(3), document_class="purchase_order", pages=3, issues=1)
        self.table = FakeJobsTable()
        self.store = ReservationStore(self.table)
        self.events = eventstore.EventStore(self.table)
        self.soap = FakeSoap(customers=CUST)
        self.lam = FakeLambda()
        monkeypatch.setattr(idp, "_client_factory", lambda: self.idp)
        monkeypatch.setattr(ei, "_store_factory", lambda: self.store)
        monkeypatch.setattr(ei, "_events_factory", lambda: self.events)
        monkeypatch.setattr(ei, "_clients_factory", lambda: self.soap)
        monkeypatch.setattr(ei, "_lambda_factory", lambda: self.lam)
        monkeypatch.setenv("SYTELINE_DEFAULT_WHSE", "MAIN")
        monkeypatch.setenv("SYTELINE_DEFAULT_ORDTYPE", "R")
        monkeypatch.setenv("WORKER_FUNCTION_NAME", "worker-fn")

    def run(self, event=None):
        return ei.process(event if event is not None else ev(), None)

    def untouched(self):
        return (self.idp.calls == [] and self.soap.calls == [] and self.table.items == {} and self.lam.invocations == [])


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


@pytest.fixture
def live(world, live_writes):
    return world


# ---- US1: the scan result summary ---------------------------------------------------------------------------------
def test_the_sample_event_reads_the_document_and_needs_review_when_a_field_is_below_its_threshold(world):
    world.idp.update(DOC, low={"cust_po"})
    out = world.run(SAMPLE)
    assert out["outcome"] == "refused" and out["errors"][0]["code"] == errors.DOCUMENT_NEEDS_REVIEW
    assert out["fields"] == ["cust_po"]
    s = out["summary"]
    assert s["document_id"] == DOC and s["state"] == "COMPLETED" and s["class"] == "purchase_order"
    assert s["pages"] == 3 and s["issues"] == 1 and s["below_threshold"] == ["cust_po"]
    assert {f["path"]: f["value"] for f in s["fields"]}["cust_po"] == "PO-1"
    assert world.table.items == {} and world.soap.calls == [] and world.lam.invocations == []  # nothing reserved or written


def test_the_summary_comes_from_the_idp_not_from_the_event(world):
    out = world.run(SAMPLE)  # the event says RUNNING / EVALUATING; the fake IDP says COMPLETED
    assert json.loads(SAMPLE["detail"]["output"])["document"]["status"] == "EVALUATING"
    assert out["summary"]["state"] == "COMPLETED"
    assert "ACME" not in json.dumps(SAMPLE)  # no PO value is in the event, yet the summary has them
    assert any(f["value"] == "ACME" for f in out["summary"]["fields"])


def test_a_usable_result_with_live_writes_off_returns_the_summary_and_writes_nothing(world):
    out = world.run()
    assert out["outcome"] == "read_only" and out["errors"][0]["code"] == errors.LIVE_WRITES_DISABLED
    assert out["summary"]["below_threshold"] == []
    assert world.table.items == {} and world.soap.calls == [] and world.lam.invocations == []


def test_the_document_is_read_from_the_idp_once_per_event(world):
    world.run()
    assert sorted(op for op, _ in world.idp.calls) == ["confidence", "metadata", "status"]


def test_the_log_has_the_document_id_and_counts_but_no_field_value(world, caplog):
    caplog.set_level(logging.INFO)
    world.idp.update(DOC, low={"cust_po"})
    world.run(SAMPLE)
    text = caplog.text
    assert DOC in text and '"fields"' in text and '"below_threshold": 1' in text
    for value in ("ACME", "PO-1", "customer_name"):
        assert value not in text


def test_the_summary_has_no_images_uris_or_raw_text(world):
    out = world.run()
    assert set(out["summary"]) == {"document_id", "state", "class", "pages", "issues", "fields", "below_threshold"}
    assert "s3://" not in json.dumps(out) and "image" not in json.dumps(out)


# ---- US2: reserve and save automatically ---------------------------------------------------------------------------
def test_a_usable_document_is_reserved_and_the_save_is_started(live):
    out = live.run()
    assert out["outcome"] == "save_accepted" and out["document_id"] == DOC and out["co_num"] == "100"
    assert [j["job"] for j in out["jobs"]] == ["5000", "5001", "5002"]
    assert len(live.lam.invocations) == 1 and live.lam.invocations[0][0] == "worker-fn"
    res = live.store.reservation_for_document(DOC)
    assert res["state"] == "save_accepted" and res["uploaded_by"] == "event-intake"
    assert res["caller_identity"] == f"event-intake:{DOC}-33b430ca" and res["request_ref"].startswith("event-")
    assert len(res["request_ref"]) <= 128
    assert res["save"]["order_date"] == "2026-10-22" and res["save"]["received_date"] == "2026-10-22"
    assert res["save"]["warehouse"] == "MAIN" and res["save"]["order_type"] == "R"
    assert [c for c in live.soap.writes() if c[0] != "invoke"] == []  # reserving writes no order, line or job
    claim = live.events.get(DOC)
    assert claim["state"] == "save_accepted" and claim["reservation_id"] == res["reservation_id"]


def test_a_25_line_document_is_reserved_in_one_run(live):
    live.idp.add("big.pdf", po_fields(25))
    out = live.run(ev(document_id="big.pdf"))
    assert out["outcome"] == "save_accepted" and len(out["jobs"]) == 25


def test_a_missing_default_is_refused_before_anything_is_reserved(live, monkeypatch):
    monkeypatch.delenv("SYTELINE_DEFAULT_WHSE")
    out = live.run()
    assert out["outcome"] == "refused" and out["errors"][0]["code"] == errors.DEFAULTS_NOT_CONFIGURED
    assert "SYTELINE_DEFAULT_WHSE" in out["errors"][0]["message"] and out["summary"]["document_id"] == DOC
    assert live.table.items == {} and live.soap.calls == []


def test_a_customer_that_is_not_matched_reserves_nothing_and_the_claim_is_released(live):
    live.soap = FakeSoap(customers=[])
    out = live.run()
    assert out["outcome"] == "read_only" and out["errors"][0]["code"] == errors.CUSTOMER_NOT_FOUND
    assert live.lam.invocations == [] and live.events.get(DOC) is None
    live.soap = FakeSoap(customers=CUST)  # fixed: the same event now goes through
    assert live.run()["outcome"] == "save_accepted"


def test_the_same_event_twice_reserves_and_saves_once(live):
    first = live.run()
    soap_calls, lambdas = len(live.soap.calls), len(live.lam.invocations)
    again = live.run()
    assert again["outcome"] == "already_processed" and again["first_outcome"]["co_num"] == first["co_num"]
    assert again["first_outcome"]["reservation_id"] == first["reservation_id"]
    assert again["summary"] == first["summary"]  # the same summary every time
    assert len(live.soap.calls) == soap_calls and len(live.lam.invocations) == lambdas


def test_a_delivery_that_finds_the_claim_taken_returns_the_first_outcome(live):
    assert live.events.claim(DOC, "another-execution")  # a concurrent delivery owns the document
    out = live.run()
    assert out["outcome"] == "already_processed" and out["first_outcome"]["state"] == "processing"
    assert live.soap.calls == [] and live.lam.invocations == []


def test_a_document_reserved_through_the_api_is_not_reserved_again(live):
    from syteline.reserve import run_reserve

    req = {"request_ref": "api-1", "confirmed": True, "uploaded_by": "jdoe", "document_id": DOC}
    run_reserve(req, live.soap, live.store, "arn")
    calls = len(live.soap.calls)
    out = live.run()
    assert out["outcome"] == "already_processed" and out["first_outcome"]["state"] == "reserved"
    assert len(live.soap.calls) == calls and live.lam.invocations == []


def test_an_uncertain_number_outcome_is_reported_and_never_replayed(live):
    live.soap = FakeSoap(customers=CUST, scenarios={("invoke", "NextWOSp", 2): "timeout_after"})
    out = live.run()
    assert out["outcome"] == "failed" and out["errors"][0]["code"] == errors.UNCERTAIN_OUTCOME
    assert out["consumed_unused"] and live.lam.invocations == []
    assert live.events.get(DOC)["state"] == "finished"  # the claim stays: a number is gone
    calls = len(live.soap.calls)
    again = live.run()
    assert again["outcome"] == "already_processed" and len(live.soap.calls) == calls


def test_a_claim_is_released_after_a_refusal_that_reserved_nothing(live):
    live.soap = FakeSoap(customers=CUST, scenarios={("invoke", "NextCoSp", 1): "unreachable"})
    out = live.run()
    assert out["outcome"] == "read_only" and out["errors"][0]["code"] == errors.ERP_UNREACHABLE
    assert live.events.get(DOC) is None and live.lam.invocations == []
    live.soap = FakeSoap(customers=CUST)
    assert live.run()["outcome"] == "save_accepted"


def test_a_failed_worker_start_leaves_the_reservation_for_the_sweeper(live):
    live.lam.fail = True
    out = live.run()
    assert out["outcome"] == "save_accepted"  # accepted and recorded; the sweeper re-drives the start
    assert live.store.reservation_for_document(DOC)["state"] == "save_accepted"


# ---- US3: events that cannot be used --------------------------------------------------------------------------------
@pytest.mark.parametrize("event,reason", [
    (ev(status="RUNNING"), "RUNNING"), (ev(status="FAILED"), "FAILED"), (ev(status="TIMED_OUT"), "TIMED_OUT"),
    (ev(status="ABORTED"), "ABORTED"), (ev(source="aws.s3"), "not a Step Functions"),
    (ev(detail_type="Other"), "not a Step Functions")])
def test_an_event_that_is_not_a_finished_execution_is_ignored_and_nothing_is_contacted(world, monkeypatch, event, reason):
    def boom(*a, **k):
        raise AssertionError("must not be created")

    for name in ("_store_factory", "_events_factory", "_clients_factory", "_lambda_factory"):
        monkeypatch.setattr(ei, name, boom)
    out = world.run(event)
    assert out["outcome"] == "ignored" and reason in out["reason"] and "summary" not in out
    assert world.untouched()


def test_the_ignored_answer_for_a_foreign_event_lists_what_was_received(world):
    out = world.run(ev(source="aws.s3", detail_type="Other"))
    assert out["received"] == {"source": "aws.s3", "detail-type": "Other"}
    assert out["expected"]["source"] == "aws.states" and "detail" in out["top_level_keys"]
    assert world.untouched()


@pytest.mark.parametrize("event,cause", [
    (ev(input_id=None, output_id=None), "neither"), (ev(input_id="a.pdf", output_id="b.pdf"), "different documents"),
    (ev(input_id="RAW:{bad"), "not valid JSON"), (ev(input_id="a b", output_id="a b"), "valid document ID")])
def test_an_event_without_a_usable_document_id_is_refused_and_nothing_is_contacted(world, event, cause):
    out = world.run(event)
    assert out["outcome"] == "refused" and out["errors"][0]["code"] == errors.EVENT_INVALID
    assert cause in out["errors"][0]["message"] and "summary" not in out
    assert world.untouched()


# ---- US4: failures are reported -------------------------------------------------------------------------------------
def test_a_document_that_does_not_exist_is_reported_by_id(world):
    out = world.run(ev(document_id="nope.pdf"))
    assert out["outcome"] == "refused" and out["errors"][0]["code"] == errors.DOCUMENT_NOT_FOUND
    assert "nope.pdf" in out["errors"][0]["message"] and "summary" not in out and world.table.items == {}


def test_a_document_that_has_not_finished_is_reported_with_its_state(world):
    world.idp.update(DOC, status="RUNNING")
    out = world.run()
    assert out["errors"][0]["code"] == errors.DOCUMENT_NOT_READY and out["state"] == "RUNNING" and "summary" not in out


def test_an_unavailable_idp_is_reported_and_nothing_is_reserved(live):
    world = live
    world.idp.raise_for[DOC] = RuntimeError("down")
    out = world.run()
    assert out["errors"][0]["code"] == errors.IDP_UNAVAILABLE and world.table.items == {} and world.soap.calls == []


def test_an_unreachable_secret_store_releases_the_claim(live, monkeypatch):
    def boom():
        raise errors.SecretStoreUnavailable("no path")

    monkeypatch.setattr(ei, "_clients_factory", boom)
    out = live.run()
    assert out["errors"][0]["code"] == errors.SECRET_STORE_UNREACHABLE and live.events.get(DOC) is None
    assert live.lam.invocations == []


def test_an_unreachable_reservation_store_is_reported(live, monkeypatch):
    def boom():
        raise RuntimeError("dynamodb down")

    monkeypatch.setattr(ei, "_store_factory", boom)
    out = live.run()
    assert out["errors"][0]["code"] == errors.STORE_UNAVAILABLE and live.soap.calls == []


def test_no_field_value_appears_outside_the_summary_in_any_response_or_stored_item(live):
    outs = [live.run(), live.run()]  # the run and its repeat
    live.idp.update(DOC, low={"cust_po"})
    live.events.release(DOC)
    live.table.items.pop(f"DOCRES#{DOC}")
    outs.append(live.run())  # a refusal
    for out in outs:
        body = {k: v for k, v in out.items() if k != "summary"}
        text = json.dumps(body)
        assert "ACME" not in text and "PO-1" not in text, out["outcome"]
    stored = json.dumps(live.table.items, default=str)
    assert "ACME" not in stored and "PO-1" not in stored
