import pytest

from fakes import FakeSql
from syteline.errors import IntegrationError
from syteline.resolver import resolve_customer

CUSTS = [
    {"cust_num": "     C1", "cust_seq": 0, "name": "Acme Aerospace", "city": "Phoenix", "state": "AZ", "zip": "85001"},
    {"cust_num": "     C2", "cust_seq": 0, "name": "ACME AEROSPACE", "city": "Tucson", "state": "AZ", "zip": "85701"},
    {"cust_num": "     C3", "cust_seq": 0, "name": "Other Co", "city": "Reno", "state": "NV", "zip": "89501"},
]


def test_single_match_with_normalization():
    sql = FakeSql(customers=CUSTS)
    out = resolve_customer(sql, {"name": " other   co ", "city": "reno"})
    assert out == {"cust_num": "C3", "cust_seq": 0}


def test_address_fields_disambiguate():
    sql = FakeSql(customers=CUSTS)
    assert resolve_customer(sql, {"name": "Acme Aerospace", "city": "Tucson"})["cust_num"] == "C2"


def test_no_match():
    with pytest.raises(IntegrationError) as ei:
        resolve_customer(FakeSql(customers=CUSTS), {"name": "Nobody"})
    assert ei.value.err["code"] == "CUSTOMER_NOT_FOUND"


def test_ambiguous_match():
    with pytest.raises(IntegrationError) as ei:
        resolve_customer(FakeSql(customers=CUSTS), {"name": "Acme Aerospace"})
    assert ei.value.err["code"] == "CUSTOMER_AMBIGUOUS"


def test_queries_are_parameterized_and_read_only():
    sql = FakeSql(customers=CUSTS)
    resolve_customer(sql, {"name": "O'Brien'; DROP TABLE x;--"}) if False else None
    try:
        resolve_customer(sql, {"name": "O'Brien'; DROP TABLE custaddr;--"})
    except IntegrationError:
        pass
    text, params = sql.queries[-1]
    assert "DROP" not in text and "O'BRIEN" in params[0] and "%s" in text
    assert sql.writes == [] and sql.allocations == []
