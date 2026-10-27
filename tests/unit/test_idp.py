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


# ---- one read for the summary and the details (specs/006) ---------------------------------------------------------
def test_po_details_with_a_pre_read_document_is_identical_and_reads_the_document_once(fake):
    fake.add(DOC, po_fields(2))
    plain = idp.po_details(DOC)
    fake.calls.clear()
    read = idp.read_document(DOC)
    assert idp.po_details(DOC, read=read) == plain
    idp.scan_summary(read)
    assert sorted(op for op, _ in fake.calls) == ["confidence", "metadata", "status"]  # each exactly once


def test_read_document_refuses_unfinished_missing_and_unavailable_documents(fake):
    fake.add(DOC, po_fields(), status="RUNNING")
    with pytest.raises(IntegrationError) as ei:
        idp.read_document(DOC)
    assert ei.value.err["code"] == errors.DOCUMENT_NOT_READY and ei.value.extra["state"] == "RUNNING"
    with pytest.raises(IntegrationError) as ei:
        idp.read_document("nope.pdf")
    assert ei.value.err["code"] == errors.DOCUMENT_NOT_FOUND
    fake.raise_for["x.pdf"] = RuntimeError("down")
    with pytest.raises(IntegrationError) as ei:
        idp.read_document("x.pdf")
    assert ei.value.err["code"] == errors.IDP_UNAVAILABLE


def test_a_failure_reading_the_confidence_is_an_unavailable_idp(fake):
    fake.add(DOC, po_fields())
    read = idp.read_document(DOC)
    fake.raise_for[DOC] = RuntimeError("down")
    with pytest.raises(IntegrationError) as ei:
        idp.po_details(DOC, read=read)
    assert ei.value.err["code"] == errors.IDP_UNAVAILABLE


def test_the_summary_lists_every_assessed_field_with_value_and_confidence_and_the_low_ones(fake):
    fake.add(DOC, po_fields(1), low={"cust_po", "lines[0].part_number"}, pages=3, issues=1, document_class="purchase_order")
    out = idp.scan_summary(idp.read_document(DOC))
    assert out["document_id"] == DOC and out["state"] == "COMPLETED" and out["class"] == "purchase_order"
    assert out["pages"] == 3 and out["issues"] == 1
    by_path = {f["path"]: f for f in out["fields"]}
    assert by_path["cust_po"] == {"path": "cust_po", "value": "PO-1", "confidence": 0.4, "meets_threshold": False}
    assert by_path["customer_name"]["confidence"] == 0.97 and by_path["customer_name"]["value"] == "ACME"
    assert by_path["lines[0].part_number"]["value"] == "A"  # a nested path is resolved to its value
    assert out["below_threshold"] == ["cust_po", "lines[0].part_number"]


def test_the_summary_has_no_bulk_content(fake):
    fake.add(DOC, po_fields(1))
    out = idp.scan_summary(idp.read_document(DOC))
    assert set(out) == {"document_id", "state", "class", "pages", "issues", "fields", "below_threshold"}
    assert all(set(f) == {"path", "value", "confidence", "meets_threshold"} for f in out["fields"])


# ---- a failed client creation names its cause, never its message (specs/006 follow-up) -----------------------------------
@pytest.mark.parametrize("exc,hint", [
    (ModuleNotFoundError("No module named 'idp_sdk'"), "IDP_REPO"), (KeyError("IDP_STACK_NAME"), "IDP_STACK_NAME"),
    (type("EndpointConnectionError", (Exception,), {})("secret-host.example"), "network path"),
    (RuntimeError("boom"), "IDP client could not be created")])
def test_a_client_that_cannot_be_created_reports_the_exception_type_and_a_hint_but_not_its_message(monkeypatch, exc, hint):
    def factory():
        raise exc

    monkeypatch.setattr(idp, "_client_factory", factory)
    with pytest.raises(IntegrationError) as ei:
        idp.read_document(DOC)
    assert ei.value.err["code"] == errors.IDP_UNAVAILABLE and ei.value.extra["cause"] == type(exc).__name__
    assert hint in ei.value.extra["hint"]
    assert str(exc) not in repr(ei.value.extra) and "secret-host" not in repr(ei.value.extra)


def test_a_failed_read_reports_the_exception_type_only(fake):
    fake.add(DOC, po_fields())
    fake.raise_for[DOC] = ConnectionError("10.1.2.3 refused")
    with pytest.raises(IntegrationError) as ei:
        idp.read_document(DOC)
    assert ei.value.err["code"] == errors.IDP_UNAVAILABLE and ei.value.extra == {"cause": "ConnectionError"}


def test_a_missing_module_is_named_in_the_refusal_and_the_log(monkeypatch, caplog):
    import logging

    def factory():
        raise ModuleNotFoundError("No module named 'pydantic_core'", name="pydantic_core")

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(idp, "_client_factory", factory)
    with pytest.raises(IntegrationError) as ei:
        idp.read_document(DOC)
    assert ei.value.extra["cause"] == "ModuleNotFoundError" and ei.value.extra["module"] == "pydantic_core"
    assert "IDP client could not be created: ModuleNotFoundError (module pydantic_core)" in caplog.text
    assert "No module named" not in repr(ei.value.extra) and "No module named" not in caplog.text  # never the message


def test_a_non_import_error_has_no_module_field(monkeypatch):
    def factory():
        raise KeyError("IDP_STACK_NAME")

    monkeypatch.setattr(idp, "_client_factory", factory)
    with pytest.raises(IntegrationError) as ei:
        idp.read_document(DOC)
    assert "module" not in ei.value.extra


def test_the_log_gets_the_sdk_message_shortened_but_the_response_never_does(fake, caplog):
    import logging

    class IDPProcessingError(Exception):
        pass

    fake.add(DOC, po_fields())
    fake.raise_for[DOC] = IDPProcessingError("Error querying document status for X:\n  AccessDenied " + "y" * 1500)
    caplog.set_level(logging.WARNING)
    with pytest.raises(IntegrationError) as ei:
        idp.read_document(DOC)
    line = next(r.getMessage() for r in caplog.records if "IDP read failed" in r.getMessage())
    assert line.startswith("IDP read failed: IDPProcessingError: Error querying document status for X: AccessDenied")
    assert len(line) <= len("IDP read failed: IDPProcessingError: ") + idp.MESSAGE_LIMIT and "\n" not in line
    assert ei.value.extra == {"cause": "IDPProcessingError"} and "AccessDenied" not in repr(ei.value.extra)


def test_a_full_authorization_sentence_with_a_key_arn_fits_the_log_message(fake, caplog):
    import logging

    class IDPProcessingError(Exception):
        pass

    key = "arn:aws-us-gov:kms:us-gov-east-1:108375521309:key/1234abcd-12ab-34cd-56ef-1234567890ab"
    message = ("Failed to get metadata: An error occurred (AccessDenied) when calling the GetObject operation: User: "
               "arn:aws-us-gov:sts::108375521309:assumed-role/sytline-connect-DirectFunctionRole-hJb015ErGOdW/sytline-connect-fn "
               "is not authorized to perform: kms:Decrypt on resource: " + key + " because no identity-based policy allows the "
               "kms:Decrypt action")
    fake.add(DOC, po_fields())
    fake.raise_for[DOC] = IDPProcessingError(message)
    caplog.set_level(logging.WARNING)
    with pytest.raises(IntegrationError):
        idp.read_document(DOC)
    assert key in caplog.text and "kms:Decrypt action" in caplog.text


# ---- the optional job number of a line (FR-006e) ---------------------------------------------------------
def test_a_line_may_carry_a_job_number_which_is_trimmed(fake):
    fake.add(DOC, po_fields(0, lines=[{"part_number": "A", "quantity": 1, "uom": "EA", "job_number": "  J123  "},
                                      {"part_number": "B", "quantity": 1, "uom": "EA"}]))
    out = idp.po_details(DOC)
    assert [ln.get("job") for ln in out["details"]["lines"]] == ["J123", None]
    _, _, used = idp_fields.map_fields(fake.docs[DOC]["fields"])
    assert "lines[0].job_number" in used and "lines[1].job_number" not in used


@pytest.mark.parametrize("value", [None, "", "   ", {"a": 1}, ["x"]])
def test_an_empty_or_unusable_job_number_means_not_supplied_and_is_never_required(fake, value):
    fake.add(DOC, po_fields(0, lines=[{"part_number": "A", "quantity": 1, "uom": "EA", "job_number": value}]))
    assert "job" not in idp.po_details(DOC)["details"]["lines"][0]


def test_the_job_number_field_name_can_be_overridden(fake, monkeypatch):
    monkeypatch.setenv("IDP_FIELD_MAP", '{"line.job": "work_order"}')
    fake.add(DOC, po_fields(0, lines=[{"part_number": "A", "quantity": 1, "uom": "EA", "job_number": "ignored",
                                       "work_order": "W9"}]))
    assert idp.po_details(DOC)["details"]["lines"][0]["job"] == "W9"


def test_a_supplied_job_number_read_with_low_confidence_needs_review(fake):
    fake.add(DOC, po_fields(0, lines=[{"part_number": "A", "quantity": 1, "uom": "EA", "job_number": "J1"}]),
             low={"lines"})
    e = refused()
    assert e.err["code"] == errors.DOCUMENT_NEEDS_REVIEW and "lines[0].job_number" in e.extra["fields"]


def test_the_fingerprint_covers_the_supplied_job_numbers(fake):
    plain = [{"part_number": "A", "quantity": 1, "uom": "EA"}, {"part_number": "B", "quantity": 1, "uom": "EA"}]
    fake.add(DOC, po_fields(0, lines=plain))
    base = idp.po_details(DOC)["fingerprint"]
    assert base == validation.fingerprint({"name": "ACME"}, "PO-1", 2)
    fake.add(DOC, po_fields(0, lines=[plain[0], dict(plain[1], job_number="J2")]))
    assert idp.po_details(DOC)["fingerprint"] == validation.fingerprint({"name": "ACME"}, "PO-1", 2, {2: "J2"}) != base


def test_the_note_sections_are_part_of_the_details_only_when_they_have_text(fake):
    fake.add(DOC, po_fields(1, customer_requests="  Verify work. ", billing_information="E-mail invoices.", shipping_information="   ",
                            erp_notes_summary="not attached"))
    out = idp.po_details(DOC)
    assert out["details"]["notes"] == [{"title": "Customer Requirements", "text": "Verify work."},
                                       {"title": "Billing Information", "text": "E-mail invoices."}]
    assert out["fingerprint"] == validation.fingerprint(out["details"]["customer"], "PO-1", 1)  # notes do not change it
    fake2 = fake.add(DOC, po_fields(1))
    assert "notes" not in idp.po_details(DOC)["details"]
