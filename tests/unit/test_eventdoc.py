import datetime
import json

import pytest

from fakes import status_change_event as ev
from syteline import errors, eventdoc
from syteline.errors import IntegrationError

SAMPLE = json.load(open(__import__("os").path.join(__import__("os").path.dirname(__file__), "..", "fixtures",
                                                   "sample_event.json")))


def invalid(event):
    with pytest.raises(IntegrationError) as ei:
        eventdoc.parse(event)
    assert ei.value.err["code"] == errors.EVENT_INVALID
    return ei.value.err["message"]


def test_the_repaired_sample_names_its_document():
    out = eventdoc.parse(SAMPLE)
    assert out == {"action": "process", "document_id": "159112.pdf",
                   "execution": SAMPLE["detail"]["name"], "event_date": "2026-10-22"}


@pytest.mark.parametrize("as_text", [False, True])
def test_the_event_may_be_an_object_or_json_text(as_text):
    assert eventdoc.parse(ev(as_text=as_text))["document_id"] == "159112.pdf"


def test_input_and_output_may_be_objects_not_text():
    event = ev()
    event["detail"]["input"] = json.loads(event["detail"]["input"])
    event["detail"]["output"] = json.loads(event["detail"]["output"])
    assert eventdoc.parse(event)["document_id"] == "159112.pdf"


def test_the_output_is_used_when_the_input_has_no_document():
    assert eventdoc.parse(ev(input_id=None, output_id="b/x.pdf"))["document_id"] == "b/x.pdf"
    assert eventdoc.parse(ev(output_id=None))["document_id"] == "159112.pdf"


@pytest.mark.parametrize("status", ["RUNNING", "FAILED", "TIMED_OUT", "ABORTED", "PENDING_REDRIVE"])
def test_a_status_other_than_succeeded_is_ignored_without_reading_the_documents(status):
    out = eventdoc.parse(ev(status=status, input_id="RAW:not json"))  # not even parsed
    assert out["action"] == "ignore" and status in out["reason"]


@pytest.mark.parametrize("kw", [{"source": "aws.s3"}, {"detail_type": "Object Created"}])
def test_a_foreign_source_or_type_is_ignored(kw):
    assert eventdoc.parse(ev(**kw))["action"] == "ignore"


def test_the_two_texts_naming_different_documents_are_refused():
    assert "different documents" in invalid(ev(input_id="a.pdf", output_id="b.pdf"))


def test_no_document_id_is_refused():
    assert "neither" in invalid(ev(input_id=None, output_id=None))


@pytest.mark.parametrize("raw", ["RAW:{not json", "RAW:[1, 2]"])
def test_text_that_is_not_a_json_object_is_refused(raw):
    assert "detail.input" in invalid(ev(input_id=raw))


@pytest.mark.parametrize("bad", ["a b.pdf", "../x.pdf", "a/../b", "x" * 257, "a;b", "é.pdf"])
def test_a_bad_document_id_is_refused(bad):
    assert "valid document ID" in invalid(ev(input_id=bad, output_id=bad))


def test_the_longest_valid_id_is_accepted():
    assert eventdoc.parse(ev(input_id="x" * 256, output_id="x" * 256))["document_id"] == "x" * 256


def test_the_event_that_is_not_json_or_not_an_object_is_refused():
    assert "not valid JSON" in invalid("{nope")
    assert "not a JSON object" in invalid("[1]")


def test_a_missing_or_bad_time_falls_back_to_today():
    today = datetime.date(2026, 1, 2)
    event = ev()
    del event["time"]
    assert eventdoc.parse(event, today=today)["event_date"] == "2026-01-02"
    event["time"] = "2026-10-22T23: 02: 29Z"  # the saved sample's mangled time still has a valid date part
    assert eventdoc.parse(event, today=today)["event_date"] == "2026-10-22"
    event["time"] = "yesterday"
    assert eventdoc.parse(event, today=today)["event_date"] == "2026-01-02"


# ---- is_status_change / unwrap (specs/006 phase 8) ----------------------------------------------------------------
def test_is_status_change_is_true_only_for_the_idp_event_whatever_its_status():
    assert eventdoc.is_status_change(ev()) and eventdoc.is_status_change(ev(status="FAILED"))
    assert eventdoc.is_status_change(ev(as_text=True))
    for other in ({}, {"dry_run": True}, ev(source="aws.s3"), ev(detail_type="Other"), "{nope", "[1]", None, 5):
        assert not eventdoc.is_status_change(other)


def test_unwrap_returns_the_event_under_an_event_key_as_object_or_text_else_the_payload():
    event = ev()
    assert eventdoc.unwrap({"event": event}) == event
    assert eventdoc.unwrap({"action": "document_event", "event": json.dumps(event)}) == event
    assert eventdoc.unwrap(event) is event
    assert eventdoc.unwrap({"event": "{nope"}) == {"event": "{nope"}
    assert eventdoc.unwrap({}) == {} and eventdoc.unwrap("text") == "text"
