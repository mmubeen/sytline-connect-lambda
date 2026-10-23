"""The example event is valid JSON after the repair (specs/006, research R3)."""

import json
import os

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
FILES = [os.path.join(ROOT, "tests", "fixtures", "sample_event.json"), os.path.join(ROOT, "sample_events.json")]


@pytest.mark.parametrize("path", FILES)
def test_the_example_event_parses_and_names_the_document(path):
    event = json.load(open(path))
    assert event["source"] == "aws.states" and event["detail-type"] == "Step Functions Execution Status Change"
    assert event["detail"]["status"] == "SUCCEEDED"
    for key in ("input", "output"):  # embedded as JSON text, like a real delivery
        assert isinstance(event["detail"][key], str)
        assert json.loads(event["detail"][key])["document"]["id"] == "159112.pdf"
    assert " " not in event["time"] and event["detail"]["executionArn"].count(" ") == 0


def test_the_repair_tool_accepts_valid_json_unchanged(tmp_path):
    import sys

    sys.path.insert(0, os.path.join(ROOT, "tests", "tools"))
    import make_sample_event

    text = open(FILES[0]).read()
    assert make_sample_event.repair(text) == json.loads(text)
