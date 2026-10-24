"""FR-011: one structured log line per request: type, identifiers, outcome, duration; no secrets."""

import json
import logging

import pytest

import lambda_function as lf
from fakes import FakeIdp, FakeJobsTable, FakeLambda, FakeSoap, po_fields
from handlers import intake, reserve, save_intake, status, sweeper, worker
from syteline import idp
from syteline.jobs import JobStore
from syteline.reservations import ReservationStore

CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]
BODY = {"request_ref": "ref-1", "confirmed": True, "uploaded_by": "jdoe", "customer": {"name": "ACME"},
        "lines": [{"item": "A", "quantity": 1, "uom": "EA"}]}


def lines(caplog):
    out = []
    for r in caplog.records:
        try:
            out.append(json.loads(r.getMessage()))
        except ValueError:
            pass
    return out


@pytest.fixture
def env(monkeypatch, caplog, live_writes):
    caplog.set_level(logging.INFO)
    store, soap = JobStore(FakeJobsTable()), FakeSoap(customers=CUST)
    soap.add_record("SLCos", CoNum="900")
    for m in (intake, status, worker, sweeper):
        monkeypatch.setattr(m, "_store_factory", lambda: store)
    monkeypatch.setattr(intake, "_lambda_factory", lambda: FakeLambda())
    monkeypatch.setattr(sweeper, "_lambda_factory", lambda: FakeLambda())
    monkeypatch.setattr(worker, "_clients_factory", lambda: (soap, {}))
    monkeypatch.setattr(lf, "_client_factory", lambda: soap)
    rstore = ReservationStore(FakeJobsTable())
    fake_idp = FakeIdp().add("batch-1/PO-1.pdf", po_fields(1))
    monkeypatch.setattr(idp, "_client_factory", lambda: fake_idp)
    for m in (reserve, save_intake):
        monkeypatch.setattr(m, "_store_factory", lambda: rstore)
    monkeypatch.setattr(reserve, "_clients_factory", lambda: soap)
    monkeypatch.setattr(save_intake, "_lambda_factory", lambda: FakeLambda())
    monkeypatch.setattr(worker, "_reservation_store_factory", lambda: rstore)
    monkeypatch.setattr(sweeper, "_reservation_store_factory", lambda: rstore)
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "w")
    return store


def test_each_handler_logs_type_identifiers_outcome_and_duration(env, caplog):
    r = intake.handler({"body": json.dumps(BODY), "requestContext": {"identity": {"userArn": "arn:x"}}}, None)
    uid = json.loads(r["body"])["upload_id"]
    worker.work_upload({"upload_id": uid}, None)
    status.handler({"pathParameters": {"id": uid}}, None)
    sweeper.sweep({}, None)
    lf.lambda_handler({"action": "get_notes", "record_type": "co", "record_key": "900"}, None)
    by_type = {l["type"]: l for l in lines(caplog)}
    assert {"create_order", "upload", "get_status", "sweep", "get_notes"} <= set(by_type)
    assert by_type["create_order"]["upload_id"] == uid and by_type["create_order"]["request_ref"] == "ref-1"
    assert by_type["create_order"]["outcome"] == "202" and by_type["create_order"]["uploaded_by"] == "jdoe"
    assert by_type["upload"]["outcome"] == "succeeded" and by_type["upload"]["co_num"] == "100"
    assert by_type["get_status"]["state"] == "succeeded"
    assert by_type["get_notes"]["record"] == ["co", "900"] and by_type["get_notes"]["ok"] is True
    # the entry line (FR-016 of feature 006) is written before the work, so it has no duration; every outcome line does
    assert all(isinstance(l["ms"], int) for t, l in by_type.items() if t != "event_entry")


def test_failures_are_logged_with_their_code(env, caplog):
    intake.handler({"body": json.dumps({"request_ref": "x"}), "requestContext": {}}, None)
    lf.lambda_handler({"action": "nope"}, None)
    logged = lines(caplog)
    assert any(l.get("type") == "create_order" and l.get("outcome") == "400" for l in logged)
    assert any(l.get("type") == "nope" and l.get("errors") == ["UNKNOWN_ACTION"] for l in logged)


def test_the_reservation_handlers_log_type_identifiers_outcome_and_duration(env, caplog):
    ident = {"requestContext": {"identity": {"userArn": "arn:x"}}}
    rb = {"request_ref": "res-1", "confirmed": True, "uploaded_by": "jdoe", "document_id": "batch-1/PO-1.pdf"}
    r = reserve.handler(dict({"body": json.dumps(rb)}, **ident), None)
    rid = json.loads(r["body"])["reservation_id"]
    sb = {"confirmed": True, "uploaded_by": "jdoe", "document_id": "batch-1/PO-1.pdf"}
    save_intake.handler(dict({"body": json.dumps(sb), "pathParameters": {"id": rid}}, **ident), None)
    worker.work_save({"reservation_id": rid}, None)
    by_type = {l["type"]: l for l in lines(caplog)}
    assert {"reserve", "save_intake", "save"} <= set(by_type)
    assert by_type["reserve"]["outcome"] == "201" and by_type["reserve"]["reservation_id"] == rid
    assert by_type["reserve"]["request_ref"] == "res-1" and by_type["reserve"]["uploaded_by"] == "jdoe"
    assert by_type["save_intake"]["outcome"] == "202" and by_type["save"]["outcome"] == "saved"
    assert by_type["save"]["co_num"] == "100"
    assert all(isinstance(by_type[t]["ms"], int) for t in ("reserve", "save_intake", "save"))


# ---- database-backed steps (feature 005, FR-014) -------------------------------------------------------------
def test_each_database_step_logs_type_outcome_duration_and_counts_and_nothing_secret(monkeypatch, caplog):
    from fakes import DB_CFG, FakeSqlServer
    from syteline import defaults, idos, numbers, resolver

    caplog.set_level(logging.INFO)
    for k in (idos.NEXT_CO_SETTING, idos.NEXT_JOB_SETTING, idos.CUSTOMER_SETTING, idos.DEFAULTS_SETTING):
        monkeypatch.setenv(k, "database")
    monkeypatch.setenv("SYTELINE_LIVE_WRITES", "true")
    FakeSqlServer(custaddr=[{"cust_num": "C1", "cust_seq": 0, "name": "ACME INC", "zip": "85001"}],
                  co_rows=[{"co_num": 1, "cust_num": "C1", "cust_seq": 0, "terms_code": "N30", "ship_code": "UPS"}]
                  ).install(monkeypatch)
    cfg = dict(DB_CFG)
    with numbers.NumberSource(FakeSoap(), cfg) as n:
        n.next_co()
    resolver.resolve_customer(FakeSoap(), {"name": "Acme Inc", "address1": "85001"}, cfg)
    defaults.read({"cust_num": "C1", "cust_seq": 0}, FakeSoap(), cfg)
    steps = {l["type"]: l for l in lines(caplog) if l.get("route") == "database"}
    assert set(steps) == {"reserve_number", "customer_search", "customer_defaults"}
    assert steps["reserve_number"]["outcome"] == "reserved" and steps["reserve_number"]["operation"] == "next_co"
    assert steps["customer_search"]["outcome"] == "match" and steps["customer_search"]["candidates"] == 1
    assert steps["customer_defaults"]["outcome"] == "found"
    assert all(isinstance(s["ms"], int) for s in steps.values())
    assert DB_CFG["SQL_PASS"] not in caplog.text and DB_CFG["SQL_USER"] not in caplog.text
    assert "ACME" not in json.dumps(list(steps.values()))  # no customer details in the step lines
