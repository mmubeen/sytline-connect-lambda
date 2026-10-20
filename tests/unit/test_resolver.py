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
