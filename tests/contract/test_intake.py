import json

import pytest

from fakes import FakeJobsTable, FakeLambda
from handlers import intake
from syteline.jobs import JobStore


@pytest.fixture
def env(monkeypatch):
    store, lam = JobStore(FakeJobsTable()), FakeLambda()
    monkeypatch.setattr(intake, "_store_factory", lambda: store)
    monkeypatch.setattr(intake, "_lambda_factory", lambda: lam)
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "worker-fn")
    return store, lam


def body(**kw):
    b = {"request_ref": "ocr-1", "confirmed": True, "uploaded_by": "jdoe",
         "customer": {"name": "ACME"}, "lines": [{"item": "A", "quantity": 1, "uom": "EA"}]}
    b.update(kw)
    return b


def event(b, arn="arn:aws:iam::1:user/app"):
    return {"body": json.dumps(b) if not isinstance(b, str) else b,
            "requestContext": {"identity": {"userArn": arn}}}


def test_202_records_job_and_invokes_worker_async(env):
    store, lam = env
    r = intake.handler(event(body()), None)
    out = json.loads(r["body"])
    assert r["statusCode"] == 202 and out["state"] == "accepted" and out["duplicate"] is False
    job = store.get_job(out["job_id"])
    assert job["uploaded_by"] == "jdoe" and job["caller_identity"] == "arn:aws:iam::1:user/app"
    assert job["request"]["customer"] == {"name": "ACME"}
    fn, mode, payload = lam.invocations[0]
    assert (fn, mode) == ("worker-fn", "Event") and json.loads(payload) == {"job_id": out["job_id"]}


def test_200_duplicate_same_body_does_not_reinvoke(env):
    store, lam = env
    first = json.loads(intake.handler(event(body()), None)["body"])
    r = intake.handler(event(body()), None)
    out = json.loads(r["body"])
    assert r["statusCode"] == 200 and out["duplicate"] is True and out["job_id"] == first["job_id"]
    assert len(lam.invocations) == 1


def test_409_same_ref_different_body(env):
    intake.handler(event(body()), None)
    r = intake.handler(event(body(order_date="2026-10-21")), None)
    assert r["statusCode"] == 409 and json.loads(r["body"])["errors"][0]["code"] == "REQUEST_REF_CONFLICT"


def test_400_lists_every_problem_and_stores_nothing(env):
    store, lam = env
    r = intake.handler(event({"request_ref": "x", "confirmed": False, "customer": {"name": "A", "cust_num": "C1"},
                              "lines": []}), None)
    fields = {e["field"] for e in json.loads(r["body"])["errors"]}
    assert r["statusCode"] == 400 and {"confirmed", "uploaded_by", "customer.cust_num", "lines"} <= fields
    assert store._t.items == {} and lam.invocations == []


def test_400_on_invalid_json(env):
    assert intake.handler(event("{nope"), None)["statusCode"] == 400


def test_invoke_failure_leaves_job_accepted_and_still_202(env, monkeypatch):
    store, _ = env
    monkeypatch.setattr(intake, "_lambda_factory", lambda: FakeLambda(fail=True))
    r = intake.handler(event(body()), None)
    out = json.loads(r["body"])
    assert r["statusCode"] == 202 and store.get_job(out["job_id"])["state"] == "accepted"


def test_503_when_job_store_is_down_and_nothing_is_acknowledged(env, monkeypatch):
    class Down:
        def create_job(self, *a, **k):
            raise RuntimeError("dynamodb down")

    monkeypatch.setattr(intake, "_store_factory", lambda: Down())
    r = intake.handler(event(body()), None)
    assert r["statusCode"] == 503 and json.loads(r["body"])["errors"][0]["code"] == "STORE_UNAVAILABLE"
