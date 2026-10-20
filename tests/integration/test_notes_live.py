"""Live add-then-get note check. SKIPPED unless SYTELINE_LIVE=1 AND LIVE_APPROVAL_REF are set.

Writes ONE real note on an existing record (LIVE_JOB). Agree the cleanup beforehand: the note text
contains a unique marker so the ERP owner can find and remove it in SyteLine afterwards.
"""

import os
import uuid

import pytest

import lambda_function as lf

pytestmark = pytest.mark.skipif(
    os.environ.get("SYTELINE_LIVE") != "1" or not os.environ.get("LIVE_APPROVAL_REF"),
    reason="needs SYTELINE_LIVE=1 and LIVE_APPROVAL_REF (explicit ERP-owner approval)")


def test_add_then_get_note():
    text = f"live test note {uuid.uuid4()} (approval {os.environ['LIVE_APPROVAL_REF']})"
    job = os.environ["LIVE_JOB"]
    added = lf.lambda_handler({"action": "add_note", "record_type": "job", "record_key": job,
                               "text": text, "internal": True}, None)
    assert added["ok"], added
    got = lf.lambda_handler({"action": "get_notes", "record_type": "job", "record_key": job}, None)
    assert any(n["text"] == text for n in got["notes"])
