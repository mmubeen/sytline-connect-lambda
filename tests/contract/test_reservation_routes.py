"""Contract tests for the job reservation routes (specs/004-job-number-workflow/contracts/api.md)."""

import json

import pytest

from fakes import FakeJobsTable, FakeLambda, FakeSoap
from handlers import reserve, save_intake
from syteline.reservations import ReservationStore

CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]
ARN = "arn:aws-us-gov:iam::1:user/app"


@pytest.fixture
def env(monkeypatch):
    store, soap, lam = ReservationStore(FakeJobsTable()), FakeSoap(customers=CUST), FakeLambda()
    monkeypatch.setattr(reserve, "_store_factory", lambda: store)
    monkeypatch.setattr(reserve, "_clients_factory", lambda: soap)
    monkeypatch.setattr(save_intake, "_store_factory", lambda: store)
    monkeypatch.setattr(save_intake, "_lambda_factory", lambda: lam)
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "worker-fn")
    return store, soap, lam


def rbody(**kw):
    b = {"request_ref": "ocr-1", "confirmed": True, "uploaded_by": "jdoe", "customer": {"name": "ACME"},
         "po_number": "PO-1", "line_count": 2}
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
    assert out["co_num"] == "100" and out["jobs"] == [{"line": 1, "job": "5000"}, {"line": 2, "job": "5001"}]
    assert out["customer_match"] == {"cust_num": "C1", "cust_seq": 0}
    assert set(out) == {"reservation_id", "state", "duplicate", "co_num", "jobs", "customer_match"}
    item = store.get(out["reservation_id"])
    assert item["uploaded_by"] == "jdoe" and item["caller_identity"] == ARN
    assert [c for c in soap.writes() if c[0] != "invoke"] == []


def test_200_duplicate_returns_the_same_numbers_without_new_ones(env, live_writes):
    store, soap, _ = env
    _, first = call(reserve.handler, event(rbody()))
    n = len(soap.writes())
    status, again = call(reserve.handler, event(rbody()))
    assert status == 200 and again["duplicate"] is True
    assert again["reservation_id"] == first["reservation_id"] and again["jobs"] == first["jobs"]
    assert len(soap.writes()) == n


def test_409_same_ref_different_request(env, live_writes):
    call(reserve.handler, event(rbody()))
    status, out = call(reserve.handler, event(rbody(po_number="PO-2")))
    assert status == 409 and out["errors"][0]["code"] == "REQUEST_REF_CONFLICT"


def test_400_lists_every_problem_and_reserves_nothing(env, live_writes):
    store, soap, _ = env
    status, out = call(reserve.handler, event({"request_ref": "", "confirmed": False,
                                               "customer": {"name": "A", "cust_num": "C1"}, "line_count": 0}))
    fields = {e["field"] for e in out["errors"]}
    assert status == 400 and {"request_ref", "confirmed", "uploaded_by", "customer.cust_num", "po_number",
                              "line_count"} <= fields
    assert store._t.items == {} and soap.writes() == []


def test_400_too_many_lines_and_bad_json(env, live_writes):
    status, out = call(reserve.handler, event(rbody(line_count=26)))
    assert status == 400 and out["errors"][0]["code"] == "TOO_MANY_LINES"
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


# -- POST /job-reservations/{id}/save ------------------------------------------------------
def sbody(**kw):
    b = {"confirmed": True, "uploaded_by": "jdoe", "customer": {"name": "ACME"}, "po_number": "PO-1",
         "lines": [{"item": "A", "quantity": 1, "uom": "EA"}, {"item": "B", "quantity": 2, "uom": "EA"}]}
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
    status, out = call(save_intake.handler, sevent(rid, sbody()))
    assert status == 202 and out == {"reservation_id": rid, "state": "save_accepted"}
    item = store.get(rid)
    assert item["state"] == "save_accepted" and item["save"]["request"]["po_number"] == "PO-1"
    assert lam.invocations and len(lam.invocations) == 1
    fn, mode, payload = lam.invocations[0]
    assert (fn, mode) == ("worker-fn", "Event") and json.loads(payload) == {"reservation_id": rid}
    assert len(soap.writes()) == writes  # accepting writes nothing to the ERP


def test_400_save_lists_every_problem_and_records_nothing(env, live_writes):
    store, _, lam = env
    rid = reserved(env)
    status, out = call(save_intake.handler, sevent(rid, {"confirmed": False, "customer": {}, "lines": []}))
    assert status == 400 and {"confirmed", "uploaded_by", "customer.name", "po_number", "lines"} <= {
        e["field"] for e in out["errors"]}
    assert store.get(rid)["state"] == "reserved" and lam.invocations == []
    status, out = call(save_intake.handler, sevent(rid, "{nope"))
    assert status == 400


def test_404_unknown_reservation(env, live_writes):
    status, out = call(save_intake.handler, sevent("no-such-id", sbody()))
    assert status == 404 and out["errors"][0]["code"] == "NOT_FOUND"
    assert call(save_intake.handler, event(sbody()))[0] == 404  # no id at all


# -- US3: a reservation is used once, for its own PO, by its own caller ------------------------
def nothing_written(env, writes_before):
    assert len(env[1].writes()) == writes_before


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


@pytest.mark.parametrize("change", [{"customer": {"name": "OTHER CO"}}, {"po_number": "PO-9"},
                                    {"lines": [{"item": "A", "quantity": 1, "uom": "EA"}]}])
def test_422_a_different_customer_po_or_line_count_is_refused(env, live_writes, change):
    store, soap, lam = env
    rid = reserved(env)
    status, out = call(save_intake.handler, sevent(rid, sbody(**change)))
    assert status == 422 and out["errors"][0]["code"] == "RESERVATION_MISMATCH"
    assert store.get(rid)["state"] == "reserved" and lam.invocations == []


def test_the_fingerprint_ignores_case_and_spacing(env, live_writes):
    rid = reserved(env)
    status, _ = call(save_intake.handler, sevent(rid, sbody(customer={"name": "acme"}, po_number="po-1")))
    assert status == 202


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
    item, _ = store.create_reservation({"request_ref": "x", "po_number": "PO-1", "line_count": 2}, "h",
                                       __import__("syteline.validation", fromlist=["x"]).fingerprint(
                                           {"name": "ACME"}, "PO-1", 2), "jdoe", ARN)
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
    assert out["jobs"] == [{"line": 1, "job": "5000", "usage": "reserved"}, {"line": 2, "job": "5001", "usage": "reserved"}]
    assert {"reservation_id", "request_ref", "po_number", "uploaded_by", "created_at", "updated_at"} <= set(out)
    assert "failure" not in out and "possibly_written" not in out


def test_status_saved_shows_used_numbers_and_per_line_results(full, live_writes):
    env_, worker = full
    rid = reserved(env_)
    call(save_intake.handler, sevent(rid, sbody()))
    assert worker.handler({"reservation_id": rid}, None) == {"state": "saved"}
    status, out = get(rid)
    assert status == 200 and out["state"] == "saved"
    assert [j["usage"] for j in out["jobs"]] == ["used", "used"]
    assert out["lines"] == [
        {"line": 1, "job": "5000", "status": "saved", "warnings": [], "co_line": 1, "item": "A"},
        {"line": 2, "job": "5001", "status": "saved", "warnings": [], "co_line": 2, "item": "B"}]


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
    worker.handler({"reservation_id": rid}, None)
    status, out = get(rid)  # the second line was rejected after the first saved: its rows are proven absent
    assert out["state"] == "partly_saved" and out["failure"]["step"] == "create_line_2"
    assert [j["usage"] for j in out["jobs"]] == ["used", "consumed_unused"] and "possibly_written" not in out
    assert [l["status"] for l in out["lines"]] == ["saved", "failed"]

    soap.scenarios[("insert", "CCTJobs", 2)] = "timeout_after"  # the 2nd job insert overall (1st run made one)
    _, o2 = call(reserve.handler, event(rbody(request_ref="ocr-2")))
    call(save_intake.handler, sevent(o2["reservation_id"], sbody()))
    worker.handler({"reservation_id": o2["reservation_id"]}, None)
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


def test_status_404_and_no_internal_fields_leak(full, live_writes):
    env_, worker = full
    assert get("nope")[0] == 404 and call(reservation_status.handler, {})[0] == 404
    rid = reserved(env_)
    call(save_intake.handler, sevent(rid, sbody()))
    worker.handler({"reservation_id": rid}, None)
    text = json.dumps(get(rid)[1])
    for hidden in ("payload_hash", "fingerprint", "caller_identity", ARN, "save", "customer", "ACME", "attempts"):
        assert hidden not in text.replace('"saved"', "").replace("save_accepted", ""), hidden
