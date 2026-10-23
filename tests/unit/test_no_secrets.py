import json
import logging

import lambda_function as lf
from fakes import FakeIdp, FakeJobsTable, FakeLambda, FakeSoap, po_fields
from handlers import intake, reserve, save_intake, status, sweeper, worker
from syteline import idp
from syteline.jobs import JobStore
from syteline.reservations import ReservationStore

SECRETS = ("S3cr3t-ERP-Pass",)


def test_no_password_in_any_response_or_log(monkeypatch, caplog, live_writes):
    monkeypatch.setenv("SYTELINE_PASS", SECRETS[0])
    monkeypatch.setenv("SYTELINE_WSDL", "http://127.0.0.1:9/x?wsdl")
    monkeypatch.setenv("SYTELINE_USER", "u")
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "w")
    caplog.set_level(logging.DEBUG)
    store = JobStore(FakeJobsTable())
    soap = FakeSoap(customers=[{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}])
    for mod in (intake, status, worker, sweeper):
        monkeypatch.setattr(mod, "_store_factory", lambda: store)
    monkeypatch.setattr(intake, "_lambda_factory", lambda: FakeLambda())
    monkeypatch.setattr(sweeper, "_lambda_factory", lambda: FakeLambda())
    monkeypatch.setattr(worker, "_clients_factory", lambda: (soap, {}))
    monkeypatch.setattr(lf, "_client_factory", lambda: soap)
    rstore = ReservationStore(FakeJobsTable())
    fake_idp = FakeIdp().add("batch-1/PO-1.pdf", po_fields(1))
    monkeypatch.setattr(idp, "_client_factory", lambda: fake_idp)
    for mod in (reserve, save_intake):
        monkeypatch.setattr(mod, "_store_factory", lambda: rstore)
    monkeypatch.setattr(reserve, "_clients_factory", lambda: soap)
    monkeypatch.setattr(save_intake, "_lambda_factory", lambda: FakeLambda())
    monkeypatch.setattr(worker, "_reservation_store_factory", lambda: rstore)
    monkeypatch.setattr(sweeper, "_reservation_store_factory", lambda: rstore)

    outs = [lf.lambda_handler({}, None), lf.lambda_handler({"action": "health", "dry_run": True}, None)]
    body = {"request_ref": "r", "confirmed": True, "uploaded_by": "u", "customer": {"name": "ACME"},
            "lines": [{"item": "A", "quantity": 1, "uom": "EA"}]}
    r = intake.handler({"body": json.dumps(body), "requestContext": {"identity": {"userArn": "arn"}}}, None)
    jid = json.loads(r["body"])["upload_id"]
    outs += [r, worker.work_upload({"upload_id": jid}, None), status.handler({"pathParameters": {"id": jid}}, None),
             sweeper.sweep({}, None)]
    ident = {"requestContext": {"identity": {"userArn": "arn"}}}
    rb = {"request_ref": "rr", "confirmed": True, "uploaded_by": "u", "document_id": "batch-1/PO-1.pdf"}
    rr = reserve.handler(dict({"body": json.dumps(rb)}, **ident), None)
    rid = json.loads(rr["body"])["reservation_id"]
    sb = {"confirmed": True, "uploaded_by": "u", "document_id": "batch-1/PO-1.pdf"}
    outs += [rr, save_intake.handler(dict({"body": json.dumps(sb), "pathParameters": {"id": rid}}, **ident), None),
             worker.work_save({"reservation_id": rid}, None)]
    text = json.dumps(outs, default=str) + caplog.text
    for secret in SECRETS:
        assert secret not in text


def test_soap_client_error_messages_do_not_leak_the_password():
    import pytest

    from syteline.client import SoapIdoClient
    from syteline.errors import ErpUnreachable

    class Svc:
        def CreateSessionToken(self, u, p, c):
            raise RuntimeError("bad login")

    class Z:
        service = Svc()

    cli = SoapIdoClient({"SYTELINE_USER": "u", "SYTELINE_PASS": SECRETS[0], "SYTELINE_CONFIG": "C"}, zeep_client=Z())
    with pytest.raises(ErpUnreachable) as ei:
        cli.connect()
    assert SECRETS[0] not in str(ei.value)


def test_no_database_login_or_password_anywhere_on_the_database_route(monkeypatch, caplog, live_writes):
    from fakes import DB_CFG, FakeSqlServer
    from syteline import health, idos

    caplog.set_level(logging.DEBUG)
    for k in (idos.NEXT_CO_SETTING, idos.NEXT_JOB_SETTING, idos.CUSTOMER_SETTING, idos.DEFAULTS_SETTING):
        monkeypatch.setenv(k, "database")
    for k, v in DB_CFG.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SYTELINE_WSDL", "http://127.0.0.1:9/x?wsdl")
    monkeypatch.setenv("SYTELINE_USER", "u")
    monkeypatch.setenv("SYTELINE_PASS", SECRETS[0])
    f = FakeSqlServer(custaddr=[{"cust_num": "C1", "cust_seq": 0, "name": "ACME", "zip": "85001"}],
                      infobar=f"login {DB_CFG['SQL_USER']} pw {DB_CFG['SQL_PASS']}").install(monkeypatch)
    fake_idp = FakeIdp().add("batch-1/PO-1.pdf", po_fields(1, ship_to_address="85001"))
    monkeypatch.setattr(idp, "_client_factory", lambda: fake_idp)
    rstore = ReservationStore(FakeJobsTable())
    soap = FakeSoap(customers=[{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}])
    monkeypatch.setattr(reserve, "_store_factory", lambda: rstore)
    monkeypatch.setattr(reserve, "_clients_factory", lambda: soap)
    ident = {"requestContext": {"identity": {"userArn": "arn"}}}
    body = {"request_ref": "rr", "confirmed": True, "uploaded_by": "u", "document_id": "batch-1/PO-1.pdf"}
    outs = [reserve.handler(dict({"body": json.dumps(body)}, **ident), None), health.run_health({"action": "health"})]
    monkeypatch.setattr(FakeSqlServer, "connect", lambda self, **kw: (_ for _ in ()).throw(OSError(
        f"refused for {kw['user']} / {kw['password']}")))
    outs.append(reserve.handler(dict({"body": json.dumps(dict(body, request_ref="r2"))}, **ident), None))
    text = json.dumps(outs, default=str) + caplog.text
    for secret in (DB_CFG["SQL_PASS"], DB_CFG["SQL_USER"], SECRETS[0]):
        assert secret not in text
    assert f.statements  # the database really was used
