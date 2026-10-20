import json
import logging

import pytest

import lambda_function as lf
from fakes import FakeJobsTable, FakeLambda, FakeSoap, FakeSql
from handlers import intake, status, sweeper, worker
from syteline.jobs import JobStore

SECRETS = ("S3cr3t-ERP-Pass", "S3cr3t-SQL-Pass")


def test_no_password_in_any_response_or_log(monkeypatch, caplog, live_writes):
    monkeypatch.setenv("SYTELINE_PASS", SECRETS[0])
    monkeypatch.setenv("SYTELINE_SQL_PASS", SECRETS[1])
    monkeypatch.setenv("SYTELINE_WSDL", "http://127.0.0.1:9/x?wsdl")
    monkeypatch.setenv("SYTELINE_USER", "u")
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "w")
    caplog.set_level(logging.DEBUG)
    store = JobStore(FakeJobsTable())
    sql, soap = FakeSql(customers=[{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]), FakeSoap()
    for mod in (intake, status, worker, sweeper):
        monkeypatch.setattr(mod, "_store_factory", lambda: store)
    monkeypatch.setattr(intake, "_lambda_factory", lambda: FakeLambda())
    monkeypatch.setattr(sweeper, "_lambda_factory", lambda: FakeLambda())
    monkeypatch.setattr(worker, "_clients_factory", lambda: (sql, soap, {}))
    monkeypatch.setattr(lf, "_sql_factory", lambda: sql)

    outs = [lf.lambda_handler({}, None), lf.lambda_handler({"action": "health", "dry_run": True}, None)]
    body = {"request_ref": "r", "confirmed": True, "uploaded_by": "u", "customer": {"name": "ACME"},
            "lines": [{"item": "A", "quantity": 1, "uom": "EA"}]}
    r = intake.handler({"body": json.dumps(body), "requestContext": {"identity": {"userArn": "arn"}}}, None)
    jid = json.loads(r["body"])["job_id"]
    outs += [r, worker.handler({"job_id": jid}, None), status.handler({"pathParameters": {"id": jid}}, None),
             sweeper.handler({}, None)]
    text = json.dumps(outs, default=str) + caplog.text
    for secret in SECRETS:
        assert secret not in text


def test_sql_client_error_messages_do_not_leak_credentials(monkeypatch):
    from syteline.errors import ErpUnreachable
    from syteline.sql import SqlClient

    def connect(cfg):
        raise RuntimeError(f"login failed for password {cfg['SYTELINE_SQL_PASS']}")

    cfg = {"SYTELINE_SQL_HOST": "h", "SYTELINE_SQL_PORT": "1", "SYTELINE_SQL_DB": "d",
           "SYTELINE_SQL_USER": "u", "SYTELINE_SQL_PASS": SECRETS[1]}
    with pytest.raises(ErpUnreachable) as ei:
        SqlClient(cfg, connect=connect).query("SELECT 1")
    assert SECRETS[1] not in str(ei.value)
