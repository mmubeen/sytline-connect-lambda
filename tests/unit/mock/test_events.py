"""T045: the processing-complete event for a document id (real-IDP mode builds it; the scenarios share it)."""

import json
import os
import subprocess
import sys

import pytest

from edge import events
from syteline import eventdoc

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def test_shape_matches_the_sample_event():
    sample = json.load(open(os.path.join(ROOT, "tests", "fixtures", "sample_event.json")))
    ev = events.build_event("159112.pdf")
    assert ev["source"] == sample["source"] == "aws.states"
    assert ev["detail-type"] == sample["detail-type"]
    assert ev["detail"]["status"] == "SUCCEEDED"
    assert set(ev["detail"]) >= {"executionArn", "stateMachineArn", "name", "status", "input", "output"}


def test_it_is_recognised_and_parsed_by_the_function():
    ev = events.build_event("159112.pdf")
    assert eventdoc.is_status_change(ev)
    assert eventdoc.parse(ev)["document_id"] == "159112.pdf"


@pytest.mark.parametrize("bad", ["", "../x", "a b", "x" * 257, "a..b"])
def test_an_invalid_document_id_is_refused(bad):
    with pytest.raises(ValueError):
        events.build_event(bad)


def test_regenerating_the_scenarios_changes_nothing():
    before = subprocess.run(["git", "status", "--porcelain", "mock/scenarios"], cwd=ROOT, capture_output=True, text=True).stdout
    subprocess.run([sys.executable, "mock/scenarios/generate.py"], cwd=ROOT, check=True)
    after = subprocess.run(["git", "status", "--porcelain", "mock/scenarios"], cwd=ROOT, capture_output=True, text=True).stdout
    assert before == after
