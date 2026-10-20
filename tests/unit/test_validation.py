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
