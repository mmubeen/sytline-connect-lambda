"""Contract tests for the job reservation routes (specs/004-job-number-workflow/contracts/api.md)."""

import json

import pytest

from fakes import DB_CFG, FakeIdp, FakeJobsTable, FakeLambda, FakeSoap, FakeSqlServer, po_fields
from handlers import reserve, save_intake
from syteline import idos, idp
from syteline.reservations import ReservationStore

CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]
ARN = "arn:aws-us-gov:iam::1:user/app"
DOC = "batch-1/PO-1.pdf"


@pytest.fixture
def fake_idp(monkeypatch):
    f = FakeIdp().add(DOC, po_fields(2))
    monkeypatch.setattr(idp, "_client_factory", lambda: f)
    return f


@pytest.fixture
def env(monkeypatch, fake_idp):
    store, soap, lam = ReservationStore(FakeJobsTable()), FakeSoap(customers=CUST), FakeLambda()
    monkeypatch.setattr(reserve, "_store_factory", lambda: store)
    monkeypatch.setattr(reserve, "_clients_factory", lambda: soap)
    monkeypatch.setattr(save_intake, "_store_factory", lambda: store)
    monkeypatch.setattr(save_intake, "_lambda_factory", lambda: lam)
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "worker-fn")
    return store, soap, lam


def rbody(**kw):
    b = {"request_ref": "ocr-1", "confirmed": True, "uploaded_by": "jdoe", "document_id": DOC}
    b.update(kw)
    return b


def event(b, arn=ARN, **extra):
    return dict({"body": json.dumps(b) if not isinstance(b, str) else b,
                 "requestContext": {"identity": {"userArn": arn}}}, **extra)


def call(handler, ev):
    r = handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


# -- POST /job-reservations ----------------------------------------------------------------
def test_201_returns_the_numbers_in_line_order(env, live_writes):
    store, soap, _ = env
    status, out = call(reserve.handler, event(rbody()))
    assert status == 201 and out["state"] == "reserved" and out["duplicate"] is False
    assert out["co_num"] == "100" and out["jobs"] == [{"line": 1, "job": "5000", "source": "allocated"},
                                                             {"line": 2, "job": "5001", "source": "allocated"}]
    assert out["customer_match"] == {"cust_num": "C1", "cust_seq": 0}
    assert (out["document_id"], out["line_count"]) == (DOC, 2)  # counted from the document
    assert set(out) == {"reservation_id", "state", "duplicate", "document_id", "line_count", "co_num",
                        "jobs", "customer_match"}
    item = store.get(out["reservation_id"])
    assert item["uploaded_by"] == "jdoe" and item["caller_identity"] == ARN and item["document_id"] == DOC
    assert [c for c in soap.writes() if c[0] != "invoke"] == []


def test_200_duplicate_returns_the_same_numbers_without_new_ones(env, live_writes):
    store, soap, _ = env
    _, first = call(reserve.handler, event(rbody()))
    n = len(soap.writes())
    status, again = call(reserve.handler, event(rbody()))
    assert status == 200 and again["duplicate"] is True
    assert again["reservation_id"] == first["reservation_id"] and again["jobs"] == first["jobs"]
    assert len(soap.writes()) == n


def test_409_same_ref_different_request(env, live_writes, fake_idp):
    call(reserve.handler, event(rbody()))
    fake_idp.add("batch-1/OTHER.pdf", po_fields(2))
    status, out = call(reserve.handler, event(rbody(document_id="batch-1/OTHER.pdf")))
    assert status == 409 and out["errors"][0]["code"] == "REQUEST_REF_CONFLICT"


def test_400_lists_every_problem_and_reserves_nothing(env, live_writes):
    store, soap, _ = env
    status, out = call(reserve.handler, event({"request_ref": "", "confirmed": False, "customer": {"name": "A"},
                                               "po_number": "P", "line_count": 2}))
    assert status == 400 and {"request_ref", "confirmed", "uploaded_by", "document_id", "customer", "po_number",
                              "line_count"} <= {e["field"] for e in out["errors"]}
    assert store._t.items == {} and soap.writes() == []
    status, out = call(reserve.handler, event("{not json"))
    assert status == 400 and out["errors"][0]["code"] == "VALIDATION"


@pytest.mark.parametrize("customers,code", [([], "CUSTOMER_NOT_FOUND"),
                                            ([{"cust_num": "C1", "cust_seq": 0, "name": "ACME"},
                                              {"cust_num": "C2", "cust_seq": 0, "name": "ACME"}],
                                             "CUSTOMER_AMBIGUOUS")])
def test_422_when_the_customer_is_not_one_clear_match(monkeypatch, env, live_writes, customers, code):
    monkeypatch.setattr(reserve, "_clients_factory", lambda: FakeSoap(customers=customers))
    status, out = call(reserve.handler, event(rbody()))
    assert status == 422 and out["errors"][0]["code"] == code and out["state"] == "failed"
    assert "consumed_unused" not in out


def test_422_live_writes_disabled_reserves_nothing(env):
    store, soap, _ = env
    status, out = call(reserve.handler, event(rbody()))
    assert status == 422 and out["errors"][0]["code"] == "LIVE_WRITES_DISABLED" and soap.writes() == []


def test_502_erp_failure_lists_the_consumed_numbers(monkeypatch, env, live_writes):
    soap = FakeSoap(customers=CUST, scenarios={("invoke", "NextWOSp", 2): "reject"})
    monkeypatch.setattr(reserve, "_clients_factory", lambda: soap)
    status, out = call(reserve.handler, event(rbody()))
    assert status == 502 and out["errors"][0]["code"] == "ERP_REJECTED" and out["state"] == "failed"
    assert out["reservation_id"] and out["consumed_unused"] == [{"kind": "co", "number": "100"},
                                                                {"kind": "job", "line": 1, "number": "5000"}]


def test_502_secret_store_unreachable(monkeypatch, env, live_writes):
    from syteline.errors import SecretStoreUnavailable

    def boom():
        raise SecretStoreUnavailable("no path to Secrets Manager")

    monkeypatch.setattr(reserve, "_clients_factory", boom)
    status, out = call(reserve.handler, event(rbody()))
    assert status == 502 and out["errors"][0]["code"] == "SECRET_STORE_UNREACHABLE"


def test_503_when_the_reservation_cannot_be_recorded(monkeypatch, env, live_writes):
    class Down(ReservationStore):
        def create_reservation(self, *a, **k):
            raise RuntimeError("table down")

    monkeypatch.setattr(reserve, "_store_factory", lambda: Down(FakeJobsTable()))
    status, out = call(reserve.handler, event(rbody()))
    assert status == 503 and out["errors"][0]["code"] == "STORE_UNAVAILABLE"
    assert env[1].writes() == []


# -- numbers from the database (feature 005) --------------------------------------------------------------
@pytest.fixture
def db_route(monkeypatch):
    monkeypatch.setenv(idos.NEXT_CO_SETTING, "database")
    monkeypatch.setenv(idos.NEXT_JOB_SETTING, "database")
    for k, v in DB_CFG.items():
        monkeypatch.setenv(k, v)  # the function's configuration (normally from the ERP secret)


def test_201_with_numbers_from_the_database(monkeypatch, env, db_route, live_writes):
    f = FakeSqlServer().install(monkeypatch)
    status, out = call(reserve.handler, event(rbody()))
    assert status == 201 and out["co_num"] == "100" and out["jobs"] == [
        {"line": 1, "job": "5000", "source": "allocated"}, {"line": 2, "job": "5001", "source": "allocated"}]
    assert f.committed == [100, 5000, 5001] and env[1].calls.count(("invoke", "NextCoSp")) == 0


def test_502_with_the_consumed_numbers_when_the_database_outcome_is_unknown(monkeypatch, env, db_route, live_writes):
    FakeSqlServer(scenarios={("batch", 2): "drop"}).install(monkeypatch)
    status, out = call(reserve.handler, event(rbody()))
    assert status == 502 and out["errors"][0]["code"] == "UNCERTAIN_OUTCOME" and out["state"] == "failed"
    assert out["consumed_unused"] == [{"kind": "co", "number": "100"}]


def test_503_when_the_database_is_not_configured_and_nothing_is_reserved(monkeypatch, env, db_route, live_writes):
    monkeypatch.setenv("SQL_CA_PEM", "")
    f = FakeSqlServer().install(monkeypatch)
    status, out = call(reserve.handler, event(rbody()))
    assert status == 503 and out["errors"][0]["code"] == "DATABASE_NOT_CONFIGURED"
    assert "SQL_CA_PEM" in out["errors"][0]["message"] and "consumed_unused" not in out and f.connect_kwargs == []


# -- customer search through the database (feature 005) ----------------------------------------------------
DB_CUSTOMERS = [
    {"cust_num": "C0006", "cust_seq": 0, "name": "ACME DELTA", "addr1": "300 SOUTH AVENUE", "city": "SPOKANE",
     "state": "WA", "zip": "99201"},
    {"cust_num": "C0007", "cust_seq": 0, "name": "ACME DELTA GROUP", "addr1": "300 SOUTH AVENUE", "city": "SPOKANE",
     "state": "WA", "zip": "99201"},
    {"cust_num": "C0010", "cust_seq": 0, "name": "ACME HYDRAULICS", "addr1": "41 LANE", "city": "DAYTON",
     "state": "OH", "zip": "45402"},
]


@pytest.mark.parametrize("fields,code", [
    ({"customer_name": "ACME DELTA", "ship_to_address": "300 South Ave Spokane WA 99201"}, "CUSTOMER_AMBIGUOUS"),
    ({"customer_name": "ACME HYDRAULICS", "ship_to_address": "9 Other Road Reno NV 89501"}, "CUSTOMER_LOW_CONFIDENCE"),
    ({"customer_name": "ZETA UNKNOWN", "ship_to_address": "12 Elm St Springfield IL 62701"}, "CUSTOMER_NOT_FOUND")])
def test_422_with_the_candidates_when_the_database_search_refuses_the_customer(monkeypatch, env, fake_idp, live_writes,
                                                                             fields, code):
    monkeypatch.setenv(idos.CUSTOMER_SETTING, "database")
    for k, v in DB_CFG.items():
        monkeypatch.setenv(k, v)
    f = FakeSqlServer(custaddr=DB_CUSTOMERS).install(monkeypatch)
    fake_idp.add(DOC, po_fields(2, **fields))
    store, soap, _ = env
    status, out = call(reserve.handler, event(rbody()))
    assert status == 422 and out["errors"][0]["code"] == code and out["state"] == "failed"
    if code != "CUSTOMER_NOT_FOUND":
        assert out["candidates"] and set(out["candidates"][0]) == {"cust_num", "cust_seq", "name", "score"}
    else:
        assert "candidates" not in out
    assert "consumed_unused" not in out and soap.writes() == []
    assert not [s for s, _ in f.statements if s.startswith("SET NOCOUNT")]  # no number was asked for


# -- the document cannot be used: nothing is reserved or recorded (FR-019, FR-022) ----------------
DOC_REFUSALS = [
    ("not_found", lambda f: f.docs.pop(DOC), 404, "DOCUMENT_NOT_FOUND", {}),
    ("not_ready", lambda f: f.update(DOC, status="HITL_IN_PROGRESS"), 409, "DOCUMENT_NOT_READY",
     {"state": "HITL_IN_PROGRESS"}),
    ("incomplete", lambda f: f.add(DOC, {"lines": []}), 422, "DOCUMENT_INCOMPLETE",
     {"fields": ["customer.name", "po_number", "lines"]}),
    ("needs_review", lambda f: f.add(DOC, po_fields(2), low={"cust_po"}), 422, "DOCUMENT_NEEDS_REVIEW",
     {"fields": ["cust_po"]}),
    ("too_many_lines", lambda f: f.add(DOC, po_fields(26)), 422, "TOO_MANY_LINES", {"fields": ["lines"]}),
    ("idp_unavailable", lambda f: f.raise_for.update({DOC: RuntimeError("down")}), 502, "IDP_UNAVAILABLE", {})]


@pytest.mark.parametrize("name,setup,status_code,code,extra", DOC_REFUSALS, ids=[d[0] for d in DOC_REFUSALS])
def test_reserve_refuses_an_unusable_document_and_reserves_nothing(env, fake_idp, live_writes, name, setup,
                                                                    status_code, code, extra):
    store, soap, _ = env
    setup(fake_idp)
    status, out = call(reserve.handler, event(rbody()))
    assert status == status_code and out["errors"][0]["code"] == code
    for k, v in extra.items():
        assert out[k] == v
    assert store._t.items == {} and soap.calls == []


# -- POST /job-reservations/{id}/save ------------------------------------------------------
def sbody(**kw):
    b = {"confirmed": True, "uploaded_by": "jdoe", "document_id": DOC}
    b.update(kw)
    return b


def reserved(env):
    _, out = call(reserve.handler, event(rbody()))
    assert out["state"] == "reserved"
    return out["reservation_id"]


def sevent(rid, b, **kw):
    return event(b, pathParameters={"id": rid}, **kw)


def test_202_accepts_the_save_and_invokes_the_worker_once_async(env, live_writes):
    store, soap, lam = env
    rid = reserved(env)
    writes = len(soap.writes())
    status, out = call(save_intake.handler, sevent(rid, sbody(warehouse="W9", order_date="2026-10-20")))
    assert status == 202 and out == {"reservation_id": rid, "state": "save_accepted"}
    item = store.get(rid)
    assert item["state"] == "save_accepted"
    assert item["save"]["document_id"] == DOC and item["save"]["warehouse"] == "W9"
    assert "requested_at" in item["save"] and "confirmed" not in item["save"] and "lines" not in item["save"]
    assert len(lam.invocations) == 1
    fn, mode, payload = lam.invocations[0]
    assert (fn, mode) == ("worker-fn", "Event") and json.loads(payload) == {"action": "work_save", "reservation_id": rid}
    assert len(soap.writes()) == writes  # accepting writes nothing to the ERP


def test_400_save_lists_every_problem_and_records_nothing(env, live_writes):
    store, _, lam = env
    rid = reserved(env)
    status, out = call(save_intake.handler, sevent(rid, {"confirmed": False, "customer": {}, "lines": []}))
    assert status == 400 and {"confirmed", "uploaded_by", "document_id", "customer", "lines"} <= {
        e["field"] for e in out["errors"]}
    assert store.get(rid)["state"] == "reserved" and lam.invocations == []
    assert call(save_intake.handler, sevent(rid, "{nope"))[0] == 400


def test_404_unknown_reservation(env, live_writes):
    status, out = call(save_intake.handler, sevent("no-such-id", sbody()))
    assert status == 404 and out["errors"][0]["code"] == "NOT_FOUND"
    assert call(save_intake.handler, event(sbody()))[0] == 404  # no id at all


# -- a reservation is used once, for its own document, by its own caller ----------------------
def test_409_second_save_is_refused_and_writes_nothing(env, live_writes):
    store, soap, lam = env
    rid = reserved(env)
    call(save_intake.handler, sevent(rid, sbody()))
    n = len(soap.writes())
    status, out = call(save_intake.handler, sevent(rid, sbody()))
    assert status == 409 and out["errors"][0]["code"] == "RESERVATION_USED" and out["state"] == "save_accepted"
    assert len(lam.invocations) == 1 and len(soap.writes()) == n


def test_409_save_after_it_finished_is_used_too(env, live_writes):
    store, _, _ = env
    rid = reserved(env)
    call(save_intake.handler, sevent(rid, sbody()))
    store.claim(rid)
    store.finish_saved(rid, [])
    status, out = call(save_intake.handler, sevent(rid, sbody()))
    assert status == 409 and out["errors"][0]["code"] == "RESERVATION_USED" and out["state"] == "saved"


def test_422_a_different_document_id_is_refused(env, live_writes, fake_idp):
    store, soap, lam = env
    rid = reserved(env)
    fake_idp.add("batch-1/OTHER.pdf", po_fields(2))
    status, out = call(save_intake.handler, sevent(rid, sbody(document_id="batch-1/OTHER.pdf")))
    assert status == 422 and out["errors"][0]["code"] == "RESERVATION_MISMATCH"
    assert store.get(rid)["state"] == "reserved" and lam.invocations == []


@pytest.mark.parametrize("changed", [po_fields(2, customer_name="OTHER CO"), po_fields(2, cust_po="PO-9"),
                                     po_fields(1)], ids=["customer", "po_number", "line_count"])
def test_422_a_document_that_no_longer_matches_the_reservation_is_refused(env, live_writes, fake_idp, changed):
    store, soap, lam = env
    rid = reserved(env)
    fake_idp.add(DOC, changed)
    status, out = call(save_intake.handler, sevent(rid, sbody()))
    assert status == 422 and out["errors"][0]["code"] == "DOCUMENT_CHANGED"
    assert store.get(rid)["state"] == "reserved" and lam.invocations == []


def test_a_correction_that_keeps_customer_po_and_line_count_is_accepted(env, live_writes, fake_idp):
    rid = reserved(env)
    fixed = po_fields(2)
    fixed["lines"][0]["quantity"] = 9  # corrected in review
    fake_idp.add(DOC, fixed)
    assert call(save_intake.handler, sevent(rid, sbody()))[0] == 202


def test_the_fingerprint_ignores_case_and_spacing(env, live_writes, fake_idp):
    rid = reserved(env)
    fake_idp.add(DOC, po_fields(2, customer_name="  acme ", cust_po="po-1"))
    assert call(save_intake.handler, sevent(rid, sbody()))[0] == 202


@pytest.mark.parametrize("name,setup,status_code,code,extra", DOC_REFUSALS, ids=[d[0] for d in DOC_REFUSALS])
def test_save_refuses_an_unusable_document_before_recording_anything(env, fake_idp, live_writes, name, setup,
                                                                     status_code, code, extra):
    store, soap, lam = env
    rid = reserved(env)
    n = len(soap.calls)
    setup(fake_idp)
    status, out = call(save_intake.handler, sevent(rid, sbody()))
    assert status == status_code and out["errors"][0]["code"] == code
    for k, v in extra.items():
        assert out[k] == v
    assert store.get(rid)["state"] == "reserved" and lam.invocations == [] and len(soap.calls) == n


def test_403_a_different_caller_or_operator_is_refused(env, live_writes):
    store, _, lam = env
    rid = reserved(env)
    status, out = call(save_intake.handler, sevent(rid, sbody(), arn="arn:aws-us-gov:iam::1:user/other"))
    assert status == 403 and out["errors"][0]["code"] == "RESERVATION_FORBIDDEN"
    status, out = call(save_intake.handler, sevent(rid, sbody(uploaded_by="someone-else")))
    assert status == 403 and out["errors"][0]["code"] == "RESERVATION_FORBIDDEN"
    assert store.get(rid)["state"] == "reserved" and lam.invocations == []


def test_the_same_role_with_another_session_is_accepted(env, live_writes):
    role = "arn:aws-us-gov:sts::1:assumed-role/OcrRole/%s"
    _, out = call(reserve.handler, event(rbody(), arn=role % "s1"))
    status, _ = call(save_intake.handler, sevent(out["reservation_id"], sbody(), arn=role % "s2"))
    assert status == 202


def test_409_not_ready_when_reserving_or_failed(env, live_writes):
    store, _, lam = env
    item, _ = store.create_reservation({"request_ref": "x", "document_id": DOC, "line_count": 2}, "h", "fp", "jdoe", ARN)
    status, out = call(save_intake.handler, sevent(item["reservation_id"], sbody()))
    assert status == 409 and out["errors"][0]["code"] == "RESERVATION_NOT_READY" and out["state"] == "reserving"
    store.mark_failed(item["reservation_id"], "ERP_REJECTED", "m", "number_co")
    status, out = call(save_intake.handler, sevent(item["reservation_id"], sbody()))
    assert status == 409 and out["errors"][0]["code"] == "RESERVATION_NOT_READY" and out["state"] == "failed"
    assert lam.invocations == []


def test_503_when_the_save_cannot_be_recorded_the_reservation_stays_usable(monkeypatch, env, live_writes):
    store, _, lam = env
    rid = reserved(env)
    real = store.accept_save
    monkeypatch.setattr(store, "accept_save", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    status, out = call(save_intake.handler, sevent(rid, sbody()))
    assert status == 503 and out["errors"][0]["code"] == "STORE_UNAVAILABLE"
    monkeypatch.setattr(store, "accept_save", real)
    assert call(save_intake.handler, sevent(rid, sbody()))[0] == 202


# -- GET /job-reservations/{id} ---------------------------------------------------------------
from handlers import reservation_status  # noqa: E402


@pytest.fixture
def full(env, monkeypatch):
    from handlers import worker

    store, soap, lam = env
    monkeypatch.setattr(reservation_status, "_store_factory", lambda: store)
    monkeypatch.setattr(worker, "_reservation_store_factory", lambda: store)
    monkeypatch.setattr(worker, "_clients_factory", lambda: (soap, {}))
    return env, worker


def get(rid):
    return call(reservation_status.handler, {"pathParameters": {"id": rid}})


def test_status_reserved_has_the_numbers_and_no_lines(full, live_writes):
    env_, _ = full
    rid = reserved(env_)
    status, out = get(rid)
    assert status == 200 and out["state"] == "reserved" and out["co_num"] == "100" and "lines" not in out
    assert out["jobs"] == [{"line": 1, "job": "5000", "source": "allocated", "usage": "reserved"},
                           {"line": 2, "job": "5001", "source": "allocated", "usage": "reserved"}]
    assert {"reservation_id", "request_ref", "document_id", "uploaded_by", "created_at", "updated_at"} <= set(out)
    assert "failure" not in out and "possibly_written" not in out


def test_status_saved_shows_used_numbers_and_per_line_results(full, live_writes):
    env_, worker = full
    rid = reserved(env_)
    call(save_intake.handler, sevent(rid, sbody()))
    assert worker.work_save({"reservation_id": rid}, None) == {"state": "saved"}
    status, out = get(rid)
    assert status == 200 and out["state"] == "saved"
    assert [j["usage"] for j in out["jobs"]] == ["used", "used"]
    assert [{k: v for k, v in ln.items() if k not in ("due_date", "due_source")} for ln in out["lines"]] == [
        {"line": 1, "job": "5000", "status": "saved", "warnings": [], "co_line": 1, "item": "R* A"},
        {"line": 2, "job": "5001", "status": "saved", "warnings": [], "co_line": 2, "item": "R* B"}]
    assert all(ln["due_source"] == "default" and ln["due_date"] for ln in out["lines"])


def test_status_accepted_save_has_empty_lines_until_the_run_records_some(full, live_writes):
    env_, _ = full
    rid = reserved(env_)
    call(save_intake.handler, sevent(rid, sbody()))
    status, out = get(rid)
    assert out["state"] == "save_accepted" and out["lines"] == []


def test_status_needs_reconciliation_and_partly_saved(full, live_writes):
    (store, soap, _), worker = full
    soap.scenarios[("insert", "CCT_Coitems", 2)] = "reject"
    rid = reserved((store, soap, None))
    call(save_intake.handler, sevent(rid, sbody()))
    worker.work_save({"reservation_id": rid}, None)
    status, out = get(rid)  # the second line was rejected after the first saved: its rows are proven absent
    assert out["state"] == "partly_saved" and out["failure"]["step"] == "create_line_2"
    assert [j["usage"] for j in out["jobs"]] == ["used", "consumed_unused"] and "possibly_written" not in out
    assert [l["status"] for l in out["lines"]] == ["saved", "failed"]

    soap.scenarios[("insert", "CCTJobs", 2)] = "timeout_after"  # the 2nd job insert overall (1st run made one)
    _, o2 = call(reserve.handler, event(rbody(request_ref="ocr-2")))
    call(save_intake.handler, sevent(o2["reservation_id"], sbody()))
    worker.work_save({"reservation_id": o2["reservation_id"]}, None)
    status, out = get(o2["reservation_id"])
    assert out["state"] == "needs_reconciliation" and out["failure"]["code"] == "UNCERTAIN_OUTCOME"
    assert {"kind": "co", "key": o2["co_num"]} in out["possibly_written"]


def test_status_failed_reservation_lists_consumed_numbers(monkeypatch, full, live_writes):
    (store, soap, _), _ = full
    soap2 = FakeSoap(customers=CUST, scenarios={("invoke", "NextWOSp", 2): "reject"})
    monkeypatch.setattr(reserve, "_clients_factory", lambda: soap2)
    _, out = call(reserve.handler, event(rbody()))
    status, view = get(out["reservation_id"])
    assert view["state"] == "failed" and view["failure"]["code"] == "ERP_REJECTED"
    assert [j["usage"] for j in view["jobs"]] == ["consumed_unused"]


def test_status_shows_the_order_level_warnings_of_the_save(monkeypatch, full, live_writes):
    (store, soap, _), worker = full
    monkeypatch.setenv(idos.DEFAULTS_SETTING, "database")  # the worker has no database settings: warning only
    rid = reserved((store, None, None))
    call(save_intake.handler, sevent(rid, sbody()))
    worker.work_save({"reservation_id": rid}, None)
    status, out = get(rid)
    assert out["state"] == "saved" and len(out["warnings"]) == 1
    assert out["warnings"][0].startswith("customer defaults not available:")


def test_no_po_detail_is_stored_anywhere_after_a_full_reserve_and_save(full, live_writes):
    (store, _, _), worker = full
    rid = reserved((store, None, None))
    call(save_intake.handler, sevent(rid, sbody(warehouse="W9")))
    worker.work_save({"reservation_id": rid}, None)
    assert store.get(rid)["state"] == "saved"
    text = repr(store._t.items)
    for detail in ("ACME", "'PO-1'", "po_number", "quantity", "part_number", "'lines'", "EA"):
        assert detail not in text, detail
    assert DOC in text  # only the document id identifies the PO


def test_status_404_and_no_internal_fields_leak(full, live_writes):
    env_, worker = full
    assert get("nope")[0] == 404 and call(reservation_status.handler, {})[0] == 404
    rid = reserved(env_)
    call(save_intake.handler, sevent(rid, sbody()))
    worker.work_save({"reservation_id": rid}, None)
    text = json.dumps(get(rid)[1])
    for hidden in ("payload_hash", "fingerprint", "caller_identity", ARN, "save", '"customer"', "ACME", "attempts"):
        assert hidden not in text.replace('"saved"', "").replace("save_accepted", ""), hidden


def test_a_supplied_job_number_is_shown_with_its_source_and_not_listed_as_consumed_on_failure(monkeypatch):
    """FR-023: the reserve response says where each job number came from; a failed reservation lists only what it consumed."""
    from handlers import reserve as h

    item = {"reservation_id": "r1", "state": "failed", "document_id": "d", "line_count": 2, "co_num": "100",
            "updated_at": 1000,
            "failure": {"code": "ERP_REJECTED", "message": "m", "step": "number_job_3"},
            "job_numbers": [{"line": 1, "job": "J-1", "source": "document"}, {"line": 2, "job": "5000", "source": "allocated"}]}
    out = h._body(item, False)
    assert [(j["job"], j["source"]) for j in out["jobs"]] == [("J-1", "document"), ("5000", "allocated")]
    assert out["consumed_unused"] == [{"kind": "co", "number": "100"}, {"kind": "job", "line": 2, "number": "5000"}]
