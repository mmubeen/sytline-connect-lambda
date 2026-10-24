"""US2: every known production failure, switched on one at a time (needs `make -C mock up`).

The expectations are deliberately about what an operator sees (the outcome, the code, the message) and about what was NOT written;
tighten them with the exact texts after the first run on a machine with Docker (specs/008 run-notes).
"""

import pytest

from .helpers import erp_saves, wait_for


def start(control, fault=None, scenario="passes-review", **params):
    event = control.scenario(scenario)["event"]
    if fault:
        control.fault(fault, **params)
    return event


def codes(payload):
    return [e["code"] for e in payload.get("errors", [])]


def run(control, event, **extra):
    r = control.invoke({**event, **extra})
    assert r["status"] == 200, r
    return r["payload"]


def test_kms_denied_names_the_key_and_the_action(control):
    key = "arn:aws-us-gov:kms:us-gov-east-1:000000000000:key/00000000-0000-4000-8000-0000000000ff"
    out = run(control, start(control, "idp.kms_denied", key_arn=key), debug=True)
    assert out["outcome"] == "refused" and "IDP_UNAVAILABLE" in codes(out)
    assert key in out["detail"] and "kms:Decrypt" in out["detail"]
    assert not erp_saves(control)


def test_lookup_function_denied_is_idp_unavailable(control):
    out = run(control, start(control, "idp.lookup_denied"))
    assert "IDP_UNAVAILABLE" in codes(out) and not erp_saves(control)
    assert any(not c["allowed"] and c["action"] == "lambda:InvokeFunction" for c in control.calls(service="aws"))


def test_document_missing_in_the_idp_is_not_ready(control):
    out = run(control, start(control, "idp.not_found"))
    assert "DOCUMENT_NOT_READY" in codes(out)


def test_secrets_store_unavailable_stops_the_run_before_any_write(control):
    out = run(control, start(control, "secrets.unavailable"))
    assert out["ok"] is False and not erp_saves(control)


@pytest.mark.parametrize("fault,params", [("sql.unreachable", {}), ("sql.login_refused", {}), ("sql.routine_error", {}),
                                          ("sql.slow", {"delay_ms": 20000})])
def test_database_faults_stop_the_reservation_and_write_nothing(control, fault, params):
    out = run(control, start(control, fault, **params))
    assert out["ok"] is False, out
    assert not erp_saves(control)
    control.clear_faults()
    control.scenario("passes-review")
    assert run(control, control.scenario("passes-review")["event"])["ok"] is True  # normal again, no leftover state


def test_erp_unreachable_leaves_the_save_for_the_sweep(control):
    event = start(control, "erp.unreachable")
    assert run(control, event)["ok"] is True       # numbers reserved, save started in the background
    assert wait_for(lambda: not erp_saves(control), timeout=30)
    control.clear_faults()
    # the sweep re-drives work that did not finish (after SWEEPER_REDRIVE_AFTER_SECONDS = 120 in the template)
    assert wait_for(lambda: (control.sweep(), erp_saves(control))[1], timeout=240, interval=15), "the sweep never completed the save"


def test_erp_slow_is_cut_off_within_the_budget(control):
    event = start(control, "erp.slow", delay_ms=60000)
    assert run(control, event)["ok"] is True
    assert not erp_saves(control)


def test_erp_row_rejection_is_not_a_save(control):
    start(control, "erp.row_rejected")
    event = control.scenario("passes-review")["event"]
    control.fault("erp.row_rejected")
    run(control, event)
    wait_for(lambda: False, timeout=20)
    assert not erp_saves(control)


def test_erp_session_expiry_is_survived_by_logging_in_again(control):
    event = start(control, "erp.session_expired", remaining=1)
    assert run(control, event)["ok"] is True
    assert wait_for(lambda: erp_saves(control), timeout=90)
    assert len([c for c in control.calls(service="erp") if c["operation"] == "CreateSessionToken"]) >= 2


def test_ERP_write_lock_held_defers_the_save_until_released(control):
    event = start(control, "lock.held")
    assert run(control, event)["ok"] is True
    wait_for(lambda: False, timeout=20)
    assert not erp_saves(control)
    control.clear_faults()
    assert wait_for(lambda: (control.sweep(), erp_saves(control))[1], timeout=240, interval=15)


def test_self_invoke_denied_means_no_background_save(control):
    out = run(control, start(control, "lambda.invoke_denied"))
    wait_for(lambda: False, timeout=15)
    assert not erp_saves(control)
    assert any(not c["allowed"] and c["fault"] == "lambda.invoke_denied" for c in control.calls(service="aws")) or out["ok"] is False


def test_clearing_a_fault_restores_normal_behaviour(control):
    event = start(control, "idp.lookup_denied")
    assert run(control, event)["ok"] is False
    control.clear_faults()
    assert run(control, control.scenario("passes-review")["event"])["ok"] is True
