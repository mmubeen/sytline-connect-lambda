"""T036 (feature 006, FR-016, SC-006, SC-009): one `event_entry` line per invocation, and the run is never changed by it."""

import json
import logging
import os

import pytest

import lambda_function as lf
from fakes import FakeIdp, FakeJobsTable, FakeLambda, FakeSoap, po_fields, status_change_event
from handlers import event_intake as ei
from syteline import eventdoc, eventlog, eventstore, idp
from syteline.reservations import ReservationStore

DOC = "159112.pdf"
SAMPLE = json.load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "sample_event.json")))
CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ZEBRA INDUSTRIES"}]
DOC_VALUES = ("ZEBRA INDUSTRIES", "PO-SECRET-77", "PART-SECRET-A")


class Ctx:
    def get_remaining_time_in_millis(self):
        return 300_000


@pytest.fixture
def world(monkeypatch):
    fields = po_fields(2, customer_name="ZEBRA INDUSTRIES", cust_po="PO-SECRET-77")
    fields["lines"][0]["part_number"] = "PART-SECRET-A"
    fake = FakeIdp().add(DOC, fields, document_class="purchase_order", pages=3, issues=1)
    table = FakeJobsTable()
    monkeypatch.setattr(idp, "_client_factory", lambda: fake)
    monkeypatch.setattr(ei, "_store_factory", lambda: ReservationStore(table))
    monkeypatch.setattr(ei, "_events_factory", lambda: eventstore.EventStore(table))
    monkeypatch.setattr(ei, "_clients_factory", lambda: FakeSoap(customers=CUST))
    monkeypatch.setattr(ei, "_lambda_factory", lambda: FakeLambda())
    monkeypatch.setenv("SYTELINE_DEFAULT_WHSE", "MAIN")
    monkeypatch.setenv("SYTELINE_DEFAULT_ORDTYPE", "R")
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "worker-fn")


def json_lines(caplog):
    out = []
    for r in caplog.records:
        try:
            out.append(json.loads(r.getMessage()))
        except ValueError:
            pass
    return out


INVOCATIONS = {
    "an IDP event": lambda: SAMPLE,
    "an API request": lambda: {"resource": "/no-such-route", "httpMethod": "GET", "headers": {"Authorization": "x"}, "body": None},
    "health": lambda: {"action": "health"},
    "add_note": lambda: {"action": "add_note", "record_type": "co", "record_key": "900", "text": "call the customer"},
    "an unknown action": lambda: {"action": "nope"},
    "an empty payload": lambda: {},
    "None": lambda: None,
    "a string": lambda: "just text",
}


@pytest.mark.parametrize("kind", sorted(INVOCATIONS))
def test_every_invocation_writes_exactly_one_entry_line_and_it_comes_first(kind, world, caplog):
    caplog.set_level(logging.INFO)
    lf.lambda_handler(INVOCATIONS[kind](), Ctx())
    lines = json_lines(caplog)
    entries = [i for i, l in enumerate(lines) if l.get("type") == "event_entry"]
    assert len(entries) == 1 and entries[0] == 0, [l.get("type") for l in lines]
    assert lines[0]["mode"] == "redacted"


@pytest.mark.parametrize("kind", ["an API request", "an unknown action", "an empty payload", "an IDP event"])
@pytest.mark.parametrize("setting", [None, "true", "false"])
def test_the_result_does_not_depend_on_the_setting(kind, setting, world, monkeypatch):
    results = {}
    for value in (None, setting):
        if value is None:
            monkeypatch.delenv("LOG_EVENTS", raising=False)
        else:
            monkeypatch.setenv("LOG_EVENTS", value)
        results[value] = json.dumps(lf.lambda_handler(INVOCATIONS[kind](), Ctx()), sort_keys=True, default=str)
    assert results[None] == results[setting]


def test_a_failure_to_build_the_line_leaves_one_warning_and_the_same_result(world, monkeypatch, caplog):
    expected = json.dumps(lf.lambda_handler({"action": "nope"}, Ctx()), sort_keys=True)
    monkeypatch.setattr(eventlog, "render", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    caplog.clear()
    caplog.set_level(logging.INFO)
    assert json.dumps(lf.lambda_handler({"action": "nope"}, Ctx()), sort_keys=True) == expected
    assert [r.getMessage() for r in caplog.records if "event_entry" in r.getMessage()] == ["event_entry could not be built: ValueError"]


def test_with_the_setting_off_no_log_line_of_a_full_intake_run_holds_a_document_value(world, caplog, monkeypatch):
    monkeypatch.delenv("LOG_EVENTS", raising=False)
    caplog.set_level(logging.DEBUG)
    out = lf.lambda_handler(SAMPLE, Ctx())
    assert out["outcome"] in ("read_only", "refused", "reserved", "save_accepted")      # the run did the work
    text = "\n".join(r.getMessage() for r in caplog.records)
    for value in DOC_VALUES + ("Korean Air", "PLATFORM-FAN BLADE", "classification_reason"):
        assert value not in text, value


def test_with_the_setting_on_the_whole_event_is_in_the_line(world, caplog, monkeypatch):
    monkeypatch.setenv("LOG_EVENTS", "true")
    monkeypatch.setenv("LOG_EVENTS_MAX_CHARS", "1000000")
    caplog.set_level(logging.INFO)
    lf.lambda_handler(SAMPLE, Ctx())
    first = json_lines(caplog)[0]
    assert first["mode"] == "whole" and first["event"]["detail"]["input"] == SAMPLE["detail"]["input"]
    assert "Korean Air" in first["event"]["detail"]["input"]


def test_with_the_setting_off_the_line_differs_from_the_event_only_in_the_redacted_parts(world, caplog, monkeypatch):
    monkeypatch.delenv("LOG_EVENTS", raising=False)
    caplog.set_level(logging.INFO)
    lf.lambda_handler(SAMPLE, Ctx())
    shown = json_lines(caplog)[0]["event"]
    for key in ("source", "detail-type", "resources", "time", "account"):
        assert shown[key] == SAMPLE[key]
    assert {k for k, v in shown["detail"].items() if v != SAMPLE["detail"][k]} == {"input", "output"}


def test_a_credential_header_never_reaches_the_log_even_with_the_setting_on(world, caplog, monkeypatch):
    monkeypatch.setenv("LOG_EVENTS", "true")
    caplog.set_level(logging.DEBUG)
    lf.lambda_handler({"resource": "/no-such-route", "httpMethod": "GET",
                       "headers": {"Authorization": "AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE", "X-Amz-Security-Token": "TOKENVALUE"}}, Ctx())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "AKIAEXAMPLE" not in text and "TOKENVALUE" not in text


def test_the_function_still_calls_the_entry_log_first(world):
    import inspect

    source = inspect.getsource(lf.lambda_handler)
    body = [l.strip() for l in source.splitlines()[1:] if l.strip()]
    assert body[0].startswith("from handlers import routing") or "eventlog.log_entry" in body[0]
    assert "eventlog.log_entry(event" in source and source.index("eventlog.log_entry") < source.index("routing.is_api_event")
