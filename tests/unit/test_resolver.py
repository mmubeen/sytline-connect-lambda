import pytest

from fakes import FakeSoap
from syteline import idos
from syteline.errors import IntegrationError
from syteline.resolver import resolve_customer

CUSTS = [
    {"cust_num": "     C1", "cust_seq": 0, "name": "Acme Aerospace", "city": "Phoenix", "state": "AZ", "postal_code": "85001"},
    {"cust_num": "     C2", "cust_seq": 0, "name": "ACME AEROSPACE", "city": "Tucson", "state": "AZ", "postal_code": "85701-1234"},
    {"cust_num": "     C3", "cust_seq": 0, "name": "Other Co", "city": "Reno", "state": "NV", "postal_code": "89501"},
    {"cust_num": "     C3", "cust_seq": 1, "name": "Other Co", "city": "Sparks", "state": "NV", "postal_code": "89431"},
]


def soap(custs=CUSTS):
    return FakeSoap(customers=custs)


def test_single_match_normalizes_case_and_spacing():
    assert resolve_customer(soap(), {"name": " other   co ", "city": "reno"}) == {"cust_num": "C3", "cust_seq": 0}


def test_stored_name_with_extra_spaces_still_matches():
    s = soap([{"cust_num": "C9", "cust_seq": 0, "name": "Big   Widget  Inc"}])
    assert resolve_customer(s, {"name": "big widget inc"})["cust_num"] == "C9"


def test_address_fields_and_zip_prefix_disambiguate():
    assert resolve_customer(soap(), {"name": "Acme Aerospace", "city": "Tucson"})["cust_num"] == "C2"
    assert resolve_customer(soap(), {"name": "Acme Aerospace", "postal_code": "85701"})["cust_num"] == "C2"


def test_only_bill_to_row_is_considered():
    with pytest.raises(IntegrationError) as ei:
        resolve_customer(soap(), {"name": "Other Co", "city": "Sparks"})
    assert ei.value.err["code"] == "CUSTOMER_NOT_FOUND"  # Sparks is a ship-to (seq 1), not bill-to


def test_no_match():
    with pytest.raises(IntegrationError) as ei:
        resolve_customer(soap(), {"name": "Nobody"})
    assert ei.value.err["code"] == "CUSTOMER_NOT_FOUND"


def test_ambiguous_match():
    with pytest.raises(IntegrationError) as ei:
        resolve_customer(soap(), {"name": "Acme Aerospace"})
    assert ei.value.err["code"] == "CUSTOMER_AMBIGUOUS"


def test_cap_reached_is_ambiguous():
    many = [{"cust_num": f"C{i}", "cust_seq": 0, "name": f"Acme {i}"} for i in range(idos.CUSTOMER_LOOKUP_CAP + 5)]
    with pytest.raises(IntegrationError) as ei:
        resolve_customer(soap(many), {"name": "Acme 3"})
    assert ei.value.err["code"] == "CUSTOMER_AMBIGUOUS"


def test_filter_is_escaped_and_lookup_is_read_only_with_live_writes_off():
    s = soap()
    with pytest.raises(IntegrationError):
        resolve_customer(s, {"name": "O'Brien' OR 1=1; DROP TABLE x;--"})
    assert s.writes() == [] and all(c[0] == "load" for c in s.calls)


# ---- the database route (feature 005) -------------------------------------------------------------------
from fakes import DB_CFG, FakeSqlServer  # noqa: E402
from syteline import errors  # noqa: E402

DB_CUSTS = [
    {"cust_num": "C0001", "cust_seq": 0, "name": "ACME AEROSPACE INC", "addr1": "1 MAIN STREET", "city": "PHOENIX",
     "state": "AZ", "zip": "85001", "country": "USA"},
    {"cust_num": "C0006", "cust_seq": 0, "name": "DELTA COMPONENTS", "addr1": "300 SOUTH AVENUE", "city": "SPOKANE",
     "state": "WA", "zip": "99201", "country": "USA"},
    {"cust_num": "C0007", "cust_seq": 0, "name": "DELTA COMPONENTS GROUP", "addr1": "300 SOUTH AVENUE",
     "city": "SPOKANE", "state": "WA", "zip": "99201", "country": "USA"},
    {"cust_num": "C0010", "cust_seq": 0, "name": "SIGMA HYDRAULICS", "addr1": "41 LANE", "city": "DAYTON",
     "state": "OH", "zip": "45402", "country": "USA"},
]


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv(idos.CUSTOMER_SETTING, "database")
    return FakeSqlServer(custaddr=DB_CUSTS).install(monkeypatch)


def via_db(customer, soap_=None):
    return resolve_customer(soap_ or soap(), customer, dict(DB_CFG))


def test_database_route_returns_the_customer_and_ship_to_and_never_asks_the_service(db):
    s = soap()
    out = via_db({"name": "Acme Aerospace Inc", "address1": "1 Main St", "city": "Phoenix", "state": "AZ",
                  "postal_code": "85001"}, s)
    assert out == {"cust_num": "C0001", "cust_seq": 0} and s.calls == []
    assert db.opened == db.closed == 1


def test_each_refusal_maps_to_its_code_and_carries_the_candidates_with_scores(db):
    with pytest.raises(IntegrationError) as amb:
        via_db({"name": "Delta Components", "address1": "300 South Ave Spokane WA 99201"})
    assert amb.value.err["code"] == errors.CUSTOMER_AMBIGUOUS and amb.value.err["step"] == "resolve_customer"
    assert {c["cust_num"] for c in amb.value.extra["candidates"]} == {"C0006", "C0007"}
    assert all(set(c) == {"cust_num", "cust_seq", "name", "score"} for c in amb.value.extra["candidates"])
    with pytest.raises(IntegrationError) as low:
        via_db({"name": "Sigma Hydraulics", "address1": "999 Nowhere Street Reno NV 89501"})
    assert low.value.err["code"] == errors.CUSTOMER_LOW_CONFIDENCE and low.value.extra["candidates"][0]["score"] < 70
    with pytest.raises(IntegrationError) as nf:
        via_db({"name": "Zeta Unknown Industries", "address1": "12 Elm Street Springfield IL 62701"})
    assert nf.value.err["code"] == errors.CUSTOMER_NOT_FOUND


def test_a_refusal_writes_and_reserves_nothing(db):
    s = soap()
    with pytest.raises(IntegrationError):
        via_db({"name": "Zeta Unknown Industries", "address1": "12 Elm Street Springfield IL 62701"}, s)
    assert s.writes() == [] and not [x for x, _ in db.statements if x.startswith("SET NOCOUNT")]


def test_database_not_configured_is_refused_before_a_connection(db):
    with pytest.raises(IntegrationError) as ei:
        resolve_customer(soap(), {"name": "Acme", "address1": "1 Main St"}, {})
    assert ei.value.err["code"] == errors.DATABASE_NOT_CONFIGURED and db.connect_kwargs == []


def test_a_database_failure_never_falls_back_to_the_web_service(monkeypatch):
    from syteline.errors import ErpUnreachable

    monkeypatch.setenv(idos.CUSTOMER_SETTING, "database")
    FakeSqlServer(connect_error="connect_fail").install(monkeypatch)
    s = soap()
    with pytest.raises(ErpUnreachable):
        via_db({"name": "Other Co", "city": "Reno", "address1": "1 St"}, s)
    assert s.calls == []  # the service was never asked


def test_a_configured_endpoint_keeps_the_web_service_route_and_the_database_is_never_opened(monkeypatch):
    monkeypatch.setenv(idos.CUSTOMER_SETTING, "SLCustAddrs")
    f = FakeSqlServer(custaddr=DB_CUSTS).install(monkeypatch)
    assert resolve_customer(soap(), {"name": " other   co ", "city": "reno"}, dict(DB_CFG)) == {"cust_num": "C3",
                                                                                                "cust_seq": 0}
    assert f.connect_kwargs == []
