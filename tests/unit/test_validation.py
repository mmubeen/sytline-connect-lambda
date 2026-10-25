import pytest

from syteline import validation
from syteline.validation import payload_hash, validate_order


def good():
    return {"request_ref": "r1", "confirmed": True, "uploaded_by": "jdoe",
            "customer": {"name": "ACME AEROSPACE", "city": "PHOENIX"},
            "lines": [{"item": "A", "quantity": 2, "uom": "EA"}]}


def fields(ev):
    return {e["field"] for e in validate_order(ev) if "field" in e}


def test_valid_minimal_and_full():
    assert validate_order(good()) == []
    ev = good()
    ev["lines"][0].update(due_date="2026-11-01", price=0, teardown="T1")
    ev.update(order_date="2026-10-20", warehouse="W", order_type="J")
    assert validate_order(ev) == []


def test_all_problems_reported_together():
    ev = {"request_ref": "", "lines": [{"item": "", "quantity": 0}], "order_date": "10/20/2026"}
    assert fields(ev) >= {"request_ref", "confirmed", "uploaded_by", "customer", "order_date",
                          "lines[0].item", "lines[0].quantity", "lines[0].uom"}


def test_request_ref_1_to_128_chars():
    ev = good(); ev["request_ref"] = "x" * 129
    assert fields(ev) == {"request_ref"}
    ev["request_ref"] = "x" * 128
    assert validate_order(ev) == []


def test_only_confirmed_true_is_accepted():
    for v in (False, None, "true", 1):
        ev = good(); ev["confirmed"] = v
        assert fields(ev) == {"confirmed"}


def test_uploaded_by_required_non_empty():
    ev = good(); ev["uploaded_by"] = "  "
    assert fields(ev) == {"uploaded_by"}
    del ev["uploaded_by"]
    assert fields(ev) == {"uploaded_by"}


def test_customer_is_details_object_never_a_number():
    ev = good(); ev["customer"] = "C001"
    assert fields(ev) == {"customer"}
    ev = good(); ev["customer"] = {"name": "ACME", "cust_num": "C001"}
    assert fields(ev) == {"customer.cust_num"}
    ev = good(); ev["customer"] = {"city": "X"}
    assert fields(ev) == {"customer.name"}


def test_lines_rules():
    ev = good(); ev["lines"] = []
    assert fields(ev) == {"lines"}
    for bad_q in (-1, 0, True, "x"):
        ev = good(); ev["lines"][0]["quantity"] = bad_q
        assert fields(ev) == {"lines[0].quantity"}
    ev = good(); ev["lines"][0]["price"] = -1
    assert fields(ev) == {"lines[0].price"}
    ev = good(); ev["lines"][0]["due_date"] = "tomorrow"
    assert fields(ev) == {"lines[0].due_date"}
    ev = good(); ev["lines"][0]["teardown"] = 5
    assert fields(ev) == {"lines[0].teardown"}


def test_non_object_body():
    assert validate_order([])[0]["code"] == "VALIDATION"


def test_payload_hash_is_stable_and_sensitive():
    a, b = good(), good()
    assert payload_hash(a) == payload_hash(dict(reversed(list(b.items()))))
    b["lines"][0]["quantity"] = 3
    assert payload_hash(a) != payload_hash(b)


# ---- job reservation requests (feature 004, revised: PO details come from the IDP document) -----
def reserve_body(**kw):
    b = {"request_ref": "r", "confirmed": True, "uploaded_by": "u", "document_id": "batch-1/PO-1.pdf"}
    b.update(kw)
    return b


def save_body(**kw):
    b = {"confirmed": True, "uploaded_by": "u", "document_id": "batch-1/PO-1.pdf"}
    b.update(kw)
    return b


def test_reserve_request_valid_and_each_problem_reported_together():
    assert validation.validate_reserve(reserve_body()) == []
    errs = validation.validate_reserve({"request_ref": "", "confirmed": False})
    assert {e["field"] for e in errs} == {"request_ref", "confirmed", "uploaded_by", "document_id"}
    assert {e["field"] for e in validation.validate_reserve(reserve_body(document_id="  "))} == {"document_id"}
    assert [e["code"] for e in validation.validate_reserve([])] == ["VALIDATION"]


@pytest.mark.parametrize("field,value", [("customer", {"name": "A"}), ("po_number", "PO-1"), ("line_count", 2),
                                         ("lines", [])])
def test_po_details_are_refused_in_both_requests_not_ignored(field, value):
    for fn, body in ((validation.validate_reserve, reserve_body(**{field: value})),
                     (validation.validate_save, save_body(**{field: value}))):
        errs = fn(body)
        assert [e["field"] for e in errs] == [field] and "read from the IDP document" in errs[0]["message"]


def test_save_request_valid_and_optional_values_checked():
    assert validation.validate_save(save_body()) == []
    ok = save_body(order_date="2026-10-20", received_date="2026-10-19", warehouse="MAIN", order_type="J")
    assert validation.validate_save(ok) == []
    bad = save_body(order_date="2026-13-01", received_date="x", warehouse="", order_type=" ")
    assert {e["field"] for e in validation.validate_save(bad)} == {"order_date", "received_date", "warehouse",
                                                                   "order_type"}
    assert {e["field"] for e in validation.validate_save({})} == {"confirmed", "uploaded_by", "document_id"}


def test_fingerprint_binds_customer_po_and_line_count_and_ignores_case_and_spacing():
    base = validation.fingerprint({"name": "Acme  Inc", "city": "Phoenix"}, "po-1", 2)
    assert base == validation.fingerprint({"name": "ACME INC", "city": "phoenix"}, "PO-1", 2)
    assert base != validation.fingerprint({"name": "ACME INC", "city": "Tucson"}, "PO-1", 2)
    assert base != validation.fingerprint({"name": "ACME INC", "city": "Phoenix"}, "PO-2", 2)
    assert base != validation.fingerprint({"name": "ACME INC", "city": "Phoenix"}, "PO-1", 3)


def test_fingerprint_without_supplied_job_numbers_is_unchanged():
    """Pinned: reservations made before the optional job number keep matching their document."""
    assert validation.fingerprint({"name": "ACME"}, "PO-1", 2) == \
        validation.fingerprint({"name": "ACME"}, "PO-1", 2, None) == validation.fingerprint({"name": "ACME"}, "PO-1", 2, {})
    assert validation.fingerprint({"name": "ACME"}, "PO-1", 2) == \
        "9ab68cd6d6039c6a055c27d24c20872f1238ff0f8166ecf47dff178b2de79196"


def test_fingerprint_covers_supplied_job_numbers_in_any_order():
    base = validation.fingerprint({"name": "ACME"}, "PO-1", 3)
    a = validation.fingerprint({"name": "ACME"}, "PO-1", 3, {1: "J1", 3: "J3"})
    assert a != base and a == validation.fingerprint({"name": "ACME"}, "PO-1", 3, {3: "J3", 1: "J1"})
    assert a != validation.fingerprint({"name": "ACME"}, "PO-1", 3, {1: "J1", 3: "J4"})
    assert a != validation.fingerprint({"name": "ACME"}, "PO-1", 3, {1: "J1", 2: "J3"})
