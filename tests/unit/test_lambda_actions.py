"""One Lambda, `action` decides; the intake is the default action (specs/006, research R11 to R16)."""

import json
import os

import pytest

import lambda_function as lf
from fakes import FakeIdp, FakeJobsTable, FakeLambda, FakeSoap, po_fields, status_change_event as ev
from handlers import event_intake as ei
from syteline import errors, eventstore, idp
from syteline.reservations import ReservationStore

SAMPLE = json.load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "sample_event.json")))
DOC = "159112.pdf"


@pytest.fixture
def plant(monkeypatch):
    """The plant shape: a jobs table is configured; the intake runs against fakes."""
    table = FakeJobsTable()
    store = ReservationStore(table)
    fake_idp = FakeIdp().add(DOC, po_fields(2), document_class="purchase_order", pages=3)
    monkeypatch.setenv("JOBS_TABLE", "jobs")
    for k in ("SYTELINE_WSDL", "SYTELINE_USER", "SYTELINE_PASS", "SYTELINE_SECRET_ARN", "SYTELINE_DRY_RUN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(idp, "_client_factory", lambda: fake_idp)
    monkeypatch.setattr(ei, "_store_factory", lambda: store)
    monkeypatch.setattr(ei, "_events_factory", lambda: eventstore.EventStore(table))
    monkeypatch.setattr(ei, "_clients_factory", lambda: FakeSoap(customers=[{"cust_num": "C1", "name": "ACME"}]))
    monkeypatch.setattr(ei, "_lambda_factory", lambda: FakeLambda())
    monkeypatch.setenv("SYTELINE_DEFAULT_WHSE", "MAIN")
    monkeypatch.setenv("SYTELINE_DEFAULT_ORDTYPE", "R")
    return table, fake_idp


def untouched(table, fake_idp):
    return table.items == {} and fake_idp.calls == []


def test_the_raw_event_with_no_action_runs_the_intake(plant):
    out = lf.lambda_handler(SAMPLE, None)
    assert out["action"] == "document_event" and out["outcome"] == "read_only"  # usable, live writes are off
    assert out["document_id"] == DOC and out["summary"]["class"] == "purchase_order" and out["ok"] is False


@pytest.mark.parametrize("make", [
    lambda e: {"action": "document_event", "event": e},
    lambda e: {"action": "document_event", "event": json.dumps(e)},
    lambda e: {**e, "action": "document_event"},
    lambda e: {"event": e},
    lambda e: json.dumps(e)])
def test_every_form_of_the_event_gives_the_same_outcome(plant, make):
    out = lf.lambda_handler(make(SAMPLE), None)
    assert out["outcome"] == "read_only" and out["document_id"] == DOC and out["action"] == "document_event"


@pytest.mark.parametrize("payload", [{}, {"dry_run": True}, {"diagnose": True}, {"record_type": "co"},
                                     ev(source="aws.s3"), ev(detail_type="Other")])
def test_a_payload_with_no_action_that_is_not_an_idp_event_is_refused_naming_health(plant, payload):
    out = lf.lambda_handler(payload, None)
    assert out["ok"] is False and out["action"] == "document_event"
    assert out["errors"][0]["code"] == errors.EVENT_INVALID and '"action": "health"' in out["errors"][0]["message"]
    assert untouched(*plant)


def test_a_status_other_than_succeeded_is_ignored_with_or_without_an_action(plant):
    for payload in (ev(status="FAILED"), {"action": "document_event", **ev(status="RUNNING")}):
        out = lf.lambda_handler(payload, None)
        assert out["outcome"] == "ignored" and out["ok"] is True
    assert untouched(*plant)


def test_a_foreign_event_with_an_explicit_document_event_is_ignored(plant):
    out = lf.lambda_handler({"action": "document_event", **ev(source="aws.s3")}, None)
    assert out["outcome"] == "ignored" and untouched(*plant)


def test_health_still_runs_when_asked_for(plant, monkeypatch):
    monkeypatch.setenv("SYTELINE_WSDL", "http://127.0.0.1:9/x?wsdl")
    out = lf.lambda_handler({"action": "health", "dry_run": True}, None)
    assert out["dry_run"] is True and out["stages"][0]["stage"] == "load" and "outcome" not in out
    assert out["routes"]


def test_an_unknown_action_is_refused_as_before(plant):
    out = lf.lambda_handler({"action": "nope"}, None)
    assert out["ok"] is False and out["errors"][0]["code"] == errors.UNKNOWN_ACTION and untouched(*plant)


def test_without_a_jobs_table_document_event_is_not_available(plant, monkeypatch):
    monkeypatch.delenv("JOBS_TABLE")  # the stand-alone deployment
    for payload in (SAMPLE, {"action": "document_event", "event": SAMPLE}):
        out = lf.lambda_handler(payload, None)
        assert out["ok"] is False and out["errors"][0]["code"] == errors.ACTION_UNAVAILABLE
    assert untouched(*plant)


def test_without_a_jobs_table_a_bare_payload_still_gets_the_health_hint(plant, monkeypatch):
    monkeypatch.delenv("JOBS_TABLE")
    out = lf.lambda_handler({}, None)
    assert out["errors"][0]["code"] == errors.EVENT_INVALID and '"action": "health"' in out["errors"][0]["message"]


def test_the_result_carries_ok_true_for_a_saved_order_and_false_for_a_refusal(plant, live_writes):
    saved = lf.lambda_handler(SAMPLE, None)
    assert saved["outcome"] == "save_accepted" and saved["ok"] is True
    plant[1].update(DOC, low={"cust_po"})
    plant[0].items.clear()
    refused = lf.lambda_handler(SAMPLE, None)
    assert refused["outcome"] == "refused" and refused["ok"] is False


def test_the_notes_actions_are_unchanged(plant, monkeypatch):
    s = FakeSoap()
    s.add_record("SLCos", CoNum="100")
    monkeypatch.setattr(lf, "_client_factory", lambda: s)
    out = lf.lambda_handler({"action": "get_notes", "record_type": "co", "record_key": "100"}, None)
    assert out["ok"] is True
