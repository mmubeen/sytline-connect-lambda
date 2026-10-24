"""US1: the sample event, end to end, against the production-like mock environment (needs `make -C mock up`)."""

import pytest

from .helpers import erp_saves, wait_for


def scenario(control, name):
    return control.scenario(name)["event"]


def outcome(result):
    assert result["status"] == 200 and not result["function_error"], result
    return result["payload"]


def test_a_passing_document_is_reserved_and_the_order_is_written_to_the_mock_erp(control):
    event = scenario(control, "passes-review")
    out = outcome(control.invoke(event))
    assert out["outcome"] in ("reserved", "save_accepted") and out["ok"] is True, out
    saves = wait_for(lambda: erp_saves(control), timeout=90)
    assert saves, "the background save never reached the mock ERP"
    ops = [c["operation"] for c in control.calls(service="erp")]
    assert ops[0] == "CreateSessionToken"


def test_the_same_event_twice_is_processed_once(control):
    event = scenario(control, "already-processed")
    assert outcome(control.invoke(event))["ok"] is True
    assert wait_for(lambda: erp_saves(control), timeout=90)
    writes = len(erp_saves(control))
    again = outcome(control.invoke(event))
    assert again["outcome"] == "already_processed"
    assert len(erp_saves(control)) == writes


@pytest.mark.parametrize("name,code", [("needs-review", "DOCUMENT_NEEDS_REVIEW"), ("incomplete", "DOCUMENT_INCOMPLETE"),
                                       ("not-found", "DOCUMENT_NOT_READY")])
def test_refused_documents_write_nothing(control, name, code):
    out = outcome(control.invoke(scenario(control, name)))
    assert out["outcome"] == "refused" and code in [e["code"] for e in out["errors"]]
    assert [c for c in control.calls(service="erp") if c["operation"] in ("SaveDataSet", "CallMethod")] == []


def test_the_health_action_reports_each_stage(control):
    control.scenario("passes-review")
    out = outcome(control.invoke({"action": "health"}))
    assert out["ok"] is True
    stages = {st["stage"]: st["ok"] for st in out["stages"]}
    assert stages and all(stages.values()) and {"wsdl", "token", "db_login"} <= set(stages)
