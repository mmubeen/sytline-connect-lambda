"""T010: call records (redacted), faults (catalogue, counts) and scenarios."""

import pytest

from edge import models


def test_redaction_removes_secret_values_at_any_depth():
    out = models.redact({"SecretString": "p", "nested": {"password": "x", "ok": 1}, "list": [{"Authorization": "a"}],
                         "SYTELINE_PASS": "q", "SQL_CA_PEM": "-----BEGIN"})
    assert out["SecretString"] == out["nested"]["password"] == out["list"][0]["Authorization"] == models.REDACTED
    assert out["SYTELINE_PASS"] == out["SQL_CA_PEM"] == models.REDACTED
    assert out["nested"]["ok"] == 1


def test_redaction_masks_credentials_in_text():
    assert "hunter2" not in models.redact_text("login failed for user x with password=hunter2 on host")


def test_call_log_sequences_filters_and_clears():
    log = models.CallLog()
    a = log.add("erp", "LoadDataSet", {"x": 1})
    b = log.add("aws", "PutItem", {"pw": "s"}, allowed=False, action="dynamodb:PutItem", resource="r")
    assert (a["seq"], b["seq"]) == (1, 2)
    assert [c["seq"] for c in log.query(service="aws")] == [2]
    assert [c["seq"] for c in log.query(since=1)] == [2]
    assert b["request_summary"]["pw"] == models.REDACTED and b["allowed"] is False
    log.clear()
    assert log.query() == [] and log.add("erp", "x", {})["seq"] == 1


def test_fault_catalogue_has_the_documented_faults():
    expected = {"idp.kms_denied", "idp.lookup_denied", "idp.not_found", "erp.unreachable", "erp.slow", "erp.row_rejected",
                "erp.session_expired", "sql.unreachable", "sql.login_refused", "sql.routine_error", "sql.slow",
                "secrets.unavailable", "lock.held", "lambda.invoke_denied"}
    assert set(models.CATALOGUE) == expected


def test_unknown_fault_lists_the_catalogue():
    with pytest.raises(models.UnknownFault) as e:
        models.FaultSet().add("nope")
    assert "idp.kms_denied" in str(e.value)


def test_fault_applies_to_its_target_and_counts_down():
    fs = models.FaultSet()
    fs.add("erp.unreachable", remaining=2)
    assert fs.take("erp.unreachable") is not None
    assert fs.take("erp.unreachable") is not None
    assert fs.take("erp.unreachable") is None  # exhausted
    assert fs.active() == []


def test_fault_without_count_lasts_until_cleared():
    fs = models.FaultSet()
    fs.add("sql.slow", params={"delay_ms": 10})
    for _ in range(5):
        assert fs.take("sql.slow")["params"] == {"delay_ms": 10}
    assert fs.clear() == 1 and fs.take("sql.slow") is None


def test_take_ignores_other_faults():
    fs = models.FaultSet()
    fs.add("erp.slow")
    assert fs.take("erp.unreachable") is None


def test_remaining_must_be_a_positive_integer():
    with pytest.raises(ValueError):
        models.FaultSet().add("erp.slow", remaining=0)


def test_scenario_validation(tmp_path):
    d = tmp_path / "s1"
    d.mkdir()
    (d / "scenario.json").write_text('{"name": "s1", "description": "d", "event": {"detail": {}}, "expect": {"outcome": "reserved"}}')
    for f in ("idp.json", "erp.json", "secrets.json"):
        (d / f).write_text("{}")
    (d / "sql.sql").write_text("-- none")
    sc = models.load_scenario(d)
    assert sc["name"] == "s1" and sc["sql"] == "-- none"
    (d / "scenario.json").write_text('{"name": "other"}')
    with pytest.raises(ValueError):
        models.load_scenario(d)
