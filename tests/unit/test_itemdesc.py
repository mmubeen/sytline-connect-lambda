"""The line description comes from the item master (feature 004, FR-025)."""

from fakes import FakeSoap
from syteline import itemdesc
from syteline.errors import ErpUnreachable


def soap_with(*rows):
    soap = FakeSoap(item_master=False)
    for item, desc in rows:
        soap.add_record("SLItemcusts", Item=item, ItmDescription=desc)
    return soap


def test_the_description_is_read_from_the_item_master_by_the_prefixed_code():
    soap = soap_with(("R* PW1234", "  Turbine blade  "))
    assert itemdesc.lookup(soap, "R* PW1234") == "Turbine blade"
    assert soap.calls == [("load", "SLItemcusts")] and soap.writes() == []


def test_the_filter_names_the_code_with_its_quotes_doubled():
    seen = {}

    class Spy:
        def read_back(self, ido, props, flt, record_cap=0):
            seen.update(ido=ido, props=props, flt=flt, cap=record_cap)
            return []

    assert itemdesc.lookup(Spy(), "R* O'NEIL") is None
    assert seen == {"ido": "SLItemcusts", "props": ["Item", "ItmDescription"], "flt": "Item = 'R* O''NEIL'", "cap": 1}


def test_no_row_a_blank_description_and_a_blank_code_give_none():
    assert itemdesc.lookup(soap_with(), "R* X") is None
    assert itemdesc.lookup(soap_with(("R* X", "   ")), "R* X") is None
    soap = soap_with()
    assert itemdesc.lookup(soap, "  ") is None and itemdesc.lookup(soap, None) is None and soap.calls == []


def test_any_error_reading_it_gives_none_and_never_raises():
    class Down(FakeSoap):
        def load(self, ido, properties, filter_="", record_cap=0):
            raise ErpUnreachable("down")

        read_back = load

    assert itemdesc.lookup(Down(), "R* X") is None

    class Odd:
        def read_back(self, *a, **k):
            raise ValueError("boom")

    assert itemdesc.lookup(Odd(), "R* X") is None
