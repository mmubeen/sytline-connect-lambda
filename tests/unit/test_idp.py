import pytest

from fakes import FakeIdp, po_fields
from syteline import errors, idp, idp_fields, validation
from syteline.errors import IntegrationError

DOC = "batch-1/PO-1.pdf"


@pytest.fixture
def fake(monkeypatch):
    f = FakeIdp()
    monkeypatch.setattr(idp, "_client_factory", lambda: f)
    return f


def refused(doc=DOC):
    with pytest.raises(IntegrationError) as ei:
        idp.po_details(doc)
    return ei.value


def test_happy_path_maps_customer_po_number_and_lines(fake):
    fake.add(DOC, po_fields(2, ship_to_address="1 Main St", date_received="10/19/2026"))
    out = idp.po_details(DOC)
    assert out["details"] == {"customer": {"name": "ACME", "address1": "1 Main St"}, "po_number": "PO-1",
                              "received_date": "2026-10-19",
                              "lines": [{"item": "A", "quantity": 1.0, "uom": "EA"},
                                        {"item": "B", "quantity": 2.0, "uom": "EA"}]}
    assert out["fingerprint"] == validation.fingerprint(out["details"]["customer"], "PO-1", 2)


@pytest.mark.parametrize("state", ["HITL_IN_PROGRESS", "FAILED", "RUNNING", "QUEUED"])
def test_a_document_that_has_not_finished_is_refused_with_its_state(fake, state):
    fake.add(DOC, po_fields(), status=state)
    e = refused()
    assert e.err["code"] == errors.DOCUMENT_NOT_READY and state in e.err["message"] and e.extra["state"] == state
    assert ("metadata", DOC) not in fake.calls  # nothing else was read


def test_unknown_document(fake):
    assert refused("batch-1/nope.pdf").err["code"] == errors.DOCUMENT_NOT_FOUND


def test_every_missing_needed_field_is_listed_together(fake):
    fake.add(DOC, {"lines": [{"part_number": "A", "quantity": 1}, {"quantity": "x"}]})
    e = refused()
    assert e.err["code"] == errors.DOCUMENT_INCOMPLETE
    assert e.extra["fields"] == ["customer.name", "po_number", "lines[0].uom", "lines[1].item", "lines[1].quantity",
                                 "lines[1].uom"]
    assert fake.calls and ("confidence", DOC) not in fake.calls


def test_no_lines_is_incomplete(fake):
    fake.add(DOC, po_fields(0))
    assert refused().extra["fields"] == ["lines"]


def test_fields_below_threshold_are_refused_and_listed_only_when_the_save_uses_them(fake):
    fake.add(DOC, po_fields(1), low={"cust_po"})
    e = refused()
    assert e.err["code"] == errors.DOCUMENT_NEEDS_REVIEW and e.extra["fields"] == ["cust_po"]
    fake.add(DOC, po_fields(1, remark="x"), low={"remark", "unrelated_field"})  # fields the save does not read
    assert idp.po_details(DOC)["details"]["po_number"] == "PO-1"


def test_a_low_confidence_line_value_is_found_by_its_top_level_name(fake):
    fake.add(DOC, po_fields(1), low={"lines"})  # the IDP assessed the whole list, not each cell
    e = refused()
    assert e.err["code"] == errors.DOCUMENT_NEEDS_REVIEW
    assert "lines" in e.extra["fields"] and "lines[0].part_number" in e.extra["fields"]


def test_more_than_25_lines_is_refused(fake):
    fake.add(DOC, po_fields(26))
    assert refused().err["code"] == errors.TOO_MANY_LINES
    fake.add(DOC, po_fields(25))
    assert len(idp.po_details(DOC)["details"]["lines"]) == 25


def test_values_are_normalised(fake):
    fake.add(DOC, po_fields(0, customer_name="  Acme   Inc ", cust_po=" po-1 ", date_received="19-Oct-2026",
                             lines=[{"part_number": " ab-1 ", "quantity": "1,250", "uom": "ea", "price": "$3.50",
                                     "due_date": "2026/11/01", "engine_sn": "E1"}]))
    d = idp.po_details(DOC)["details"]
    assert d["customer"] == {"name": "Acme Inc"} and d["po_number"] == "po-1" and d["received_date"] == "2026-10-19"
    assert d["lines"] == [{"item": "ab-1", "quantity": 1250.0, "uom": "EA", "price": 3.5, "due_date": "2026-11-01",
                           "engine_sn": "E1"}]


def test_unreadable_values_are_named_not_guessed(fake):
    fake.add(DOC, po_fields(0, date_received="soon",
                             lines=[{"part_number": "A", "quantity": 0, "uom": "EA", "price": -1}]))
    assert refused().extra["fields"] == ["received_date", "lines[0].quantity", "lines[0].price"]


def test_document_class_is_checked_when_one_is_configured(fake, monkeypatch):
    monkeypatch.setenv("IDP_DOCUMENT_CLASS", "PurchaseOrder")
    fake.add(DOC, po_fields(1), document_class="Invoice")
    e = refused()
    assert e.err["code"] == errors.DOCUMENT_INCOMPLETE and e.extra["fields"] == ["document_class"]
    fake.add(DOC, po_fields(1), document_class="PurchaseOrder")
    assert idp.po_details(DOC)["details"]["po_number"] == "PO-1"


def test_the_field_names_can_be_overridden_without_a_code_change(fake, monkeypatch):
    monkeypatch.setenv("IDP_FIELD_MAP", '{"customer.name": "buyer", "po_number": "po", "line.item": "sku"}')
    fake.add(DOC, {"buyer": "ACME", "po": "P9", "lines": [{"sku": "X", "quantity": 1, "uom": "EA"}]})
    d = idp.po_details(DOC)["details"]
    assert d["customer"]["name"] == "ACME" and d["po_number"] == "P9" and d["lines"][0]["item"] == "X"


def test_any_other_failure_is_idp_unavailable_and_leaks_nothing(fake):
    fake.add(DOC, po_fields())
    fake.raise_for[DOC] = RuntimeError("AccessDenied arn:aws:iam::1:role/secret-role")
    e = refused()
    assert e.err["code"] == errors.IDP_UNAVAILABLE and "secret-role" not in e.err["message"]


def test_a_client_that_cannot_be_created_is_idp_unavailable(monkeypatch):
    def boom():
        raise KeyError("IDP_STACK_NAME")

    monkeypatch.setattr(idp, "_client_factory", boom)
    assert refused().err["code"] == errors.IDP_UNAVAILABLE


def test_the_fingerprint_changes_with_the_customer_po_or_line_count(fake):
    fake.add(DOC, po_fields(2))
    a = idp.po_details(DOC)["fingerprint"]
    fake.add(DOC, po_fields(3))
    assert idp.po_details(DOC)["fingerprint"] != a
    fake.add(DOC, po_fields(2, cust_po="PO-2"))
    assert idp.po_details(DOC)["fingerprint"] != a
    fake.add(DOC, po_fields(2, customer_name="OTHER"))
    assert idp.po_details(DOC)["fingerprint"] != a
