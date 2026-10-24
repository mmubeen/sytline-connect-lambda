"""T034 (feature 006, FR-016): the entry log line: redaction, masking, cap, odd payloads."""

import copy
import json
import logging

import pytest

from syteline import eventlog

SAMPLE = json.load(open("tests/fixtures/sample_event.json"))


def line_of(event, environ=None):
    return json.loads(eventlog.render(event, environ or {}))


def test_the_redacted_keys_are_exactly_these():
    assert eventlog.REDACT_KEYS == frozenset({"input", "output", "body", "text"})  # adding a key is a deliberate change


def test_an_idp_events_input_and_output_become_sizes_and_the_input_keeps_the_document_id():
    out = line_of(SAMPLE)
    assert out["type"] == "event_entry" and out["mode"] == "redacted"
    detail = out["event"]["detail"]
    assert detail["input"] == {"redacted": True, "chars": len(SAMPLE["detail"]["input"]), "document_id": "159112.pdf"}
    assert detail["output"] == {"redacted": True, "chars": len(SAMPLE["detail"]["output"])}


def test_identifiers_are_kept():
    ev = line_of(SAMPLE)["event"]
    assert ev["source"] == "aws.states" and ev["detail-type"] == "Step Functions Execution Status Change"
    assert ev["detail"]["status"] == "SUCCEEDED" and ev["detail"]["executionArn"] == SAMPLE["detail"]["executionArn"]
    assert ev["resources"] == SAMPLE["resources"]


def test_no_document_content_survives_the_default_form():
    text = eventlog.render(SAMPLE, {})
    for word in ("Korean Air", "PLATFORM-FAN BLADE", "classification_reason", "Order Number"):
        assert word not in text


API = {"resource": "/confirmed-orders", "httpMethod": "POST", "pathParameters": None,
       "headers": {"Authorization": "AWS4-HMAC-SHA256 Credential=AKIA...", "X-Amz-Security-Token": "tok", "Content-Type": "application/json",
                   "cookie": "a=b", "x-api-key": "k"},
       "body": json.dumps({"customer": {"name": "ACME"}, "lines": []}), "requestContext": {"identity": {"userArn": "arn:x"}}}


def test_an_api_request_body_is_a_size_and_routing_fields_stay():
    ev = line_of(API)["event"]
    assert ev["body"] == {"redacted": True, "chars": len(API["body"])}
    assert ev["resource"] == "/confirmed-orders" and ev["httpMethod"] == "POST"
    assert "ACME" not in eventlog.render(API, {})


@pytest.mark.parametrize("environ", [{}, {"LOG_EVENTS": "true"}])
def test_credential_headers_are_masked_in_both_modes(environ):
    text = eventlog.render(API, environ)
    for secret in ("AWS4-HMAC-SHA256", "AKIA", '"tok"', "a=b", '"k"'):
        assert secret not in text
    assert json.loads(text)["event"]["headers"]["Content-Type"] == "application/json"
    assert json.loads(text)["event"]["headers"]["Authorization"] == "***"


def test_add_note_text_is_a_size():
    ev = line_of({"action": "add_note", "record_type": "co", "record_key": "900", "text": "call the customer"})["event"]
    assert ev["text"] == {"redacted": True, "chars": 17} and ev["record_key"] == "900"


def test_redaction_works_at_any_depth_and_does_not_modify_the_input():
    original = {"a": {"b": [{"body": "secret text"}]}, "action": "x"}
    keep = copy.deepcopy(original)
    ev = line_of(original)["event"]
    assert ev["a"]["b"][0]["body"] == {"redacted": True, "chars": 11} and original == keep


def test_the_non_string_value_of_a_redacted_key_is_measured_as_json():
    assert line_of({"body": {"k": "v"}})["event"]["body"] == {"redacted": True, "chars": len(json.dumps({"k": "v"}))}


def test_whole_mode_logs_the_event_as_received():
    # the example event is far larger than the default cap, so the cap is raised here; the default cut is tested below
    out = json.loads(eventlog.render(SAMPLE, {"LOG_EVENTS": "true", "LOG_EVENTS_MAX_CHARS": "1000000"}))
    assert out["mode"] == "whole" and out["event"]["detail"]["input"] == SAMPLE["detail"]["input"]


def test_the_example_event_in_whole_mode_is_cut_at_the_default_cap():
    out = json.loads(eventlog.render(SAMPLE, {"LOG_EVENTS": "true"}))
    assert out["event"]["truncated"] is True and len(out["event"]["head"]) == 8000


@pytest.mark.parametrize("value", ["TRUE", " true ", "True"])
def test_the_setting_is_read_leniently(value):
    assert json.loads(eventlog.render({"a": 1}, {"LOG_EVENTS": value}))["mode"] == "whole"


@pytest.mark.parametrize("value", ["", "false", "yes", "1", "no"])
def test_anything_but_true_is_the_default(value):
    assert json.loads(eventlog.render({"a": 1}, {"LOG_EVENTS": value}))["mode"] == "redacted"


def test_over_the_cap_the_line_says_so_and_stays_one_valid_object():
    big = {"action": "x", "note": "y" * 20000}
    out = json.loads(eventlog.render(big, {"LOG_EVENTS": "true"}))
    assert out["event"]["truncated"] is True and out["event"]["chars"] > 20000 and len(out["event"]["head"]) == 8000


def test_the_cap_can_be_changed_and_a_bad_value_falls_back():
    big = {"note": "y" * 500}
    assert len(json.loads(eventlog.render(big, {"LOG_EVENTS": "true", "LOG_EVENTS_MAX_CHARS": "100"}))["event"]["head"]) == 100
    assert "truncated" not in json.loads(eventlog.render(big, {"LOG_EVENTS": "true", "LOG_EVENTS_MAX_CHARS": "lots"}))["event"]


def test_the_default_form_is_capped_too():
    out = json.loads(eventlog.render({"resources": ["r" * 20000]}, {}))
    assert out["event"]["truncated"] is True


@pytest.mark.parametrize("payload,expected", [
    (None, {"payload_type": "NoneType", "chars": 0}),
    (b"\xff\xfe", {"payload_type": "bytes", "chars": 2}),
    ("not json", {"payload_type": "str", "chars": 8}),
    ([1, 2], {"payload_type": "list", "chars": 6}),
    (42, {"payload_type": "int", "chars": 2}),
])
def test_payloads_that_are_not_objects_are_described(payload, expected):
    assert line_of(payload)["event"] == expected


def test_a_json_string_is_read_like_an_object():
    ev = line_of(json.dumps({"action": "get_notes", "text": "abc"}))["event"]
    assert ev["action"] == "get_notes" and ev["text"] == {"redacted": True, "chars": 3}


def test_bytes_holding_json_are_read_like_an_object():
    assert line_of(b'{"action": "health"}')["event"] == {"action": "health"}


def test_values_json_cannot_serialise_do_not_raise():
    import datetime

    out = line_of({"when": datetime.datetime(2026, 10, 24), "action": "x"})["event"]
    assert out["action"] == "x" and "2026" in out["when"]


def test_log_entry_writes_one_info_line(caplog):
    caplog.set_level(logging.INFO)
    eventlog.log_entry({"action": "health"}, logging.getLogger("t"), {})
    assert [json.loads(r.getMessage())["type"] for r in caplog.records] == ["event_entry"]


def test_log_entry_never_raises_and_leaves_one_short_warning(monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def boom(*a, **k):
        raise RuntimeError("secret detail that must not be logged")

    monkeypatch.setattr(eventlog, "render", boom)
    eventlog.log_entry({"action": "health"}, logging.getLogger("t"), {})
    assert [r.getMessage() for r in caplog.records] == ["event_entry could not be built: RuntimeError"]
