import logging

import pytest

from fakes import DB_CFG, FakeSoap, FakeSqlServer
from syteline import defaults, idos

CUST = {"cust_num": "C1", "cust_seq": 0}
CO = [
    {"co_num": 1, "cust_num": "C1", "cust_seq": 0, "terms_code": "N30", "ship_code": "UPS"},
    {"co_num": 2, "cust_num": "C1", "cust_seq": 0, "terms_code": "N45", "ship_code": "FDX"},
    {"co_num": 9, "cust_num": "C1", "cust_seq": 3, "terms_code": "N10", "ship_code": "DHL"},
    {"co_num": 5, "cust_num": "C2", "cust_seq": 0, "terms_code": "N60", "ship_code": "USPS"},
]


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "database")
    return FakeSqlServer(co_rows=CO).install(monkeypatch)


def read(customer=CUST, soap=None, cfg=None):
    return defaults.read(customer, soap or FakeSoap(), dict(DB_CFG) if cfg is None else cfg)


def test_same_ship_to_order_first_newest_wins(db):
    assert read() == ({"terms_code": "N45", "ship_code": "FDX", "source": "same_ship_to"}, None)


def test_other_ship_to_orders_when_none_at_this_one(db):
    got, warning = read({"cust_num": "C1", "cust_seq": 7})
    assert got == {"terms_code": "N10", "ship_code": "DHL", "source": "any_order"} and warning is None


def test_a_customer_without_orders_gets_no_defaults_and_a_warning(db):
    assert read({"cust_num": "C9", "cust_seq": 0}) == (None, defaults.NO_ORDER)


def test_a_failing_first_query_falls_through_within_the_database_route_only(monkeypatch):
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "database")
    FakeSqlServer(co_rows=CO, scenarios={("select", 1): "error"}).install(monkeypatch)
    soap = FakeSoap()
    got, _ = read(soap=soap)
    assert got["source"] == "any_order" and soap.calls == []  # the service was never asked


def test_a_database_failure_is_a_warning_never_an_error_and_never_switches_route(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "database")
    FakeSqlServer(connect_error="connect_fail").install(monkeypatch)
    soap = FakeSoap()
    got, warning = read(soap=soap)
    assert got is None and warning.startswith("customer defaults not available:") and soap.calls == []
    assert "S3cr3t-DB-Pass" not in warning + caplog.text


def test_missing_database_settings_are_a_warning_and_open_no_connection(db):
    got, warning = read(cfg={})
    assert got is None and "database settings missing" in warning and db.connect_kwargs == []


def test_web_service_route_picks_the_newest_order_client_side(monkeypatch):
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "SLCos")
    f = FakeSqlServer(co_rows=CO).install(monkeypatch)
    soap = FakeSoap()
    for co, seq, terms, ship in (("     9", 3, "N10", "DHL"), ("    10", 0, "N45", "FDX"), ("     2", 0, "N30", "UPS")):
        soap.add_record("SLCos", CoNum=co, CustNum="     C1", CustSeq=seq, TermsCode=terms, ShipCode=ship)
    # "     C1" is how the service pads customer numbers; the filter compares trimmed, case-insensitive text
    got, warning = read({"cust_num": "C1", "cust_seq": 0}, soap)
    assert got == {"terms_code": "N45", "ship_code": "FDX", "source": "same_ship_to"} and warning is None
    assert f.connect_kwargs == []  # the database was never opened
    got, _ = read({"cust_num": "C1", "cust_seq": 8}, soap)
    assert got["source"] == "any_order" and got["terms_code"] == "N45"  # CoNum 10 is the newest overall


def test_web_service_failure_is_a_warning_and_never_opens_the_database(monkeypatch):
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "SLCos")
    f = FakeSqlServer(co_rows=CO).install(monkeypatch)
    got, warning = read(soap=FakeSoap(read_fail=True))
    assert got is None and warning.startswith("customer defaults not available:") and f.connect_kwargs == []


def test_blank_codes_count_as_no_codes(monkeypatch):
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "database")
    FakeSqlServer(co_rows=[{"co_num": 1, "cust_num": "C1", "cust_seq": 0, "terms_code": " ", "ship_code": ""}]
                  ).install(monkeypatch)
    assert read() == (None, defaults.NO_ORDER)


def test_an_absent_setting_reads_no_defaults_and_says_nothing(monkeypatch):
    monkeypatch.delenv(idos.DEFAULTS_SETTING, raising=False)
    f = FakeSqlServer(co_rows=CO).install(monkeypatch)
    soap = FakeSoap()
    assert read(soap=soap) == (None, None) and soap.calls == [] and f.connect_kwargs == []
