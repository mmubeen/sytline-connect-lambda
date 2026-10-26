"""Development mode (feature 009): the setting and the target checks."""

import pytest

from fakes import FakeSoap
from syteline import config, devmode, errors, idos
from syteline.errors import IntegrationError

CUSTOMER = {"cust_num": "C1", "cust_seq": 0}


def soap_with(target="129999", cust="C1"):
    soap = FakeSoap()
    soap.add_record(idos.CO_IDO, CoNum=target.rjust(10), CustNum=cust.rjust(7))
    return soap


@pytest.mark.parametrize("value,expected", [(None, ""), ("", ""), ("  ", ""), ("129999", "129999"), (" 42 ", "42"), ("1234567890", "1234567890")])
def test_the_setting_is_a_digit_string_or_off(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("DEV_TARGET_CO", raising=False)
    else:
        monkeypatch.setenv("DEV_TARGET_CO", value)
    assert config.dev_target_co() == expected and devmode.active() == expected


@pytest.mark.parametrize("value", ["12345678901", "12a", "-1", "1 2", "TEST"])
def test_a_bad_setting_raises_and_is_never_a_silent_fall_back(monkeypatch, value):
    monkeypatch.setenv("DEV_TARGET_CO", value)
    with pytest.raises(IntegrationError) as exc:
        config.dev_target_co()
    assert exc.value.err["code"] == errors.DEV_TARGET_INVALID
    assert devmode.active() == ""  # logging never raises


def test_an_existing_target_of_the_documents_customer_passes():
    devmode.check_target(soap_with(), "129999", CUSTOMER)


def test_a_missing_target_names_the_order():
    with pytest.raises(IntegrationError) as exc:
        devmode.check_target(FakeSoap(), "129999", CUSTOMER)
    assert exc.value.err["code"] == errors.DEV_TARGET_MISSING and "129999" in exc.value.err["message"]


def test_a_target_of_another_customer_names_both():
    with pytest.raises(IntegrationError) as exc:
        devmode.check_target(soap_with(cust="OTHER"), "129999", CUSTOMER)
    err = exc.value.err
    assert err["code"] == errors.DEV_TARGET_CUSTOMER_MISMATCH and "OTHER" in err["message"] and "C1" in err["message"]


def test_the_check_only_reads():
    soap = soap_with()
    devmode.check_target(soap, "129999", CUSTOMER)
    assert soap.writes() == []


def test_an_event_claim_is_overwritten_not_refused():
    from fakes import FakeJobsTable
    from syteline import eventstore

    events = eventstore.EventStore(FakeJobsTable())
    assert events.claim("d1", "e1") and not events.claim("d1", "e2")
    events.claim_dev("d1", "e3", "129999")
    item = events.get("d1")
    assert item["execution"] == "e3" and item["dev_target"] == "129999" and item["state"] == "processing"


def test_every_log_line_of_a_development_run_names_the_target(monkeypatch, caplog):
    import logging
    import time

    from handlers import _common

    monkeypatch.setenv("DEV_TARGET_CO", "129999")
    with caplog.at_level(logging.INFO):
        _common.log_event("save", time.monotonic(), outcome="saved")
    assert '"dev_target": "129999"' in caplog.text
    caplog.clear()
    monkeypatch.delenv("DEV_TARGET_CO")
    with caplog.at_level(logging.INFO):
        _common.log_event("save", time.monotonic(), outcome="saved")
    assert "dev_target" not in caplog.text


def test_a_target_with_no_customer_yet_is_allowed_with_a_warning():
    soap = FakeSoap()
    soap.add_record(idos.CO_IDO, CoNum="129556".rjust(10), CustNum="")
    note = devmode.check_target(soap, "129556", CUSTOMER)
    assert "no customer yet" in note and "C1" in note
    assert devmode.check_target(soap_with(), "129999", CUSTOMER) is None
