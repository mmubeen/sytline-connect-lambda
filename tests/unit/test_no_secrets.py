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
    outs += [r, worker.handler({"upload_id": jid}, None), status.handler({"pathParameters": {"id": jid}}, None),
             sweeper.handler({}, None)]
    ident = {"requestContext": {"identity": {"userArn": "arn"}}}
    rb = {"request_ref": "rr", "confirmed": True, "uploaded_by": "u", "document_id": "batch-1/PO-1.pdf"}
    rr = reserve.handler(dict({"body": json.dumps(rb)}, **ident), None)
    rid = json.loads(rr["body"])["reservation_id"]
    sb = {"confirmed": True, "uploaded_by": "u", "document_id": "batch-1/PO-1.pdf"}
    outs += [rr, save_intake.handler(dict({"body": json.dumps(sb), "pathParameters": {"id": rid}}, **ident), None),
             worker.handler({"reservation_id": rid}, None)]
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
