"""T011: the control API of contracts/control-api.md."""

import pytest
from werkzeug.test import Client

import render_env
from edge import app as edge_app
from stubs import StubBackend, StubForward, StubRuntime


@pytest.fixture
def world():
    backend, runtime = StubBackend(), StubRuntime()
    edge = edge_app.Edge(render_env.render(), StubForward(), runtime, backend)
    return edge, Client(edge), backend, runtime


def j(client, method, path, **kw):
    r = getattr(client, method)("/__mock__" + path, environ_overrides={"REMOTE_ADDR": "127.0.0.1"}, **kw)
    return r.status_code, r.get_json()


def test_health_reports_services(world):
    _, c, _, _ = world
    assert j(c, "get", "/health") == (200, {"ready": True, "services": {"moto": "up", "erp": "up", "sql": "up", "fn": "up"}})


def test_health_not_ready_when_a_service_is_down(world):
    _, c, backend, _ = world
    backend.services["sql"] = "down"
    code, body = j(c, "get", "/health")
    assert code == 200 and body["ready"] is False and body["services"]["sql"] == "down"


def test_reset_clears_everything(world):
    edge, c, backend, _ = world
    edge.calls.add("erp", "x", {})
    edge.faults.add("erp.slow")
    assert j(c, "post", "/reset") == (200, {"reset": True})
    assert backend.resets == 1 and edge.calls.query() == [] and edge.faults.active() == []


def test_scenarios_and_load(world):
    _, c, backend, _ = world
    assert j(c, "get", "/scenarios") == (200, ["passes-review", "needs-review"])
    code, body = j(c, "post", "/scenario/passes-review")
    assert code == 200 and body["loaded"] == "passes-review" and body["event"]["detail"]["name"] == "passes-review"
    assert backend.resets == 1  # loading implies a reset
    assert j(c, "post", "/scenario/nope")[0] == 404


def test_fault_add_and_clear(world):
    edge, c, _, _ = world
    code, body = j(c, "post", "/faults", json={"id": "erp.slow", "params": {"delay_ms": 5}, "remaining": 3})
    assert code == 200 and body["fault_id"] == "erp.slow"
    assert edge.faults.peek("erp.slow")["remaining"] == 3
    assert j(c, "delete", "/faults/erp.slow") == (200, {"cleared": 1})
    j(c, "post", "/faults", json={"id": "sql.slow"})
    j(c, "post", "/faults", json={"id": "erp.slow"})
    assert j(c, "delete", "/faults") == (200, {"cleared": 2})


def test_unknown_fault_is_400_and_lists_the_catalogue(world):
    _, c, _, _ = world
    code, body = j(c, "post", "/faults", json={"id": "nope"})
    assert code == 400 and "idp.kms_denied" in body["error"]


def test_bad_fault_body_is_400(world):
    _, c, _, _ = world
    assert j(c, "post", "/faults", json={"id": "erp.slow", "remaining": 0})[0] == 400
    assert j(c, "post", "/faults", json={})[0] == 400


def test_lock_held_fault_reaches_the_backend_and_clears_it(world):
    _, c, backend, _ = world
    j(c, "post", "/faults", json={"id": "lock.held"})
    assert backend.lock_held is True
    j(c, "delete", "/faults")
    assert backend.lock_held is False


def test_calls_filter_by_service_and_since(world):
    edge, c, _, _ = world
    edge.calls.add("erp", "LoadDataSet", {})
    edge.calls.add("aws", "dynamodb:PutItem", {})
    edge.calls.add("erp", "SaveDataSet", {})
    assert [x["operation"] for x in j(c, "get", "/calls?service=erp")[1]] == ["LoadDataSet", "SaveDataSet"]
    assert [x["seq"] for x in j(c, "get", "/calls?since=2")[1]] == [3]


def test_invoke_sync_and_async(world):
    _, c, _, runtime = world
    code, body = j(c, "post", "/invoke", json={"event": {"action": "health"}, "timeout": 7})
    assert code == 200 and body["payload"] == {"ok": True}
    assert runtime.sync == [({"action": "health"}, 7)]
    code, body = j(c, "post", "/invoke", json={"event": {"action": "sweep"}, "mode": "async"})
    assert code == 200 and body["status"] == 202 and runtime.async_ == [{"action": "sweep"}]


def test_invoke_requires_an_event_object(world):
    _, c, _, _ = world
    assert j(c, "post", "/invoke", json={"event": "x"})[0] == 400


def test_sweep_tick_sends_the_schedule_input(world):
    _, c, _, runtime = world
    assert j(c, "post", "/tick/sweep")[0] == 200
    assert runtime.sync[0][0] == {"action": "sweep"}


def test_requests_from_outside_the_local_network_are_refused(world):
    _, c, _, _ = world
    r = c.get("/__mock__/health", environ_overrides={"REMOTE_ADDR": "8.8.8.8"})
    assert r.status_code == 403
    assert c.get("/__mock__/health", environ_overrides={"REMOTE_ADDR": "172.18.0.5"}).status_code == 200


def test_not_ready_environment_refuses_scenarios_with_409(world):
    _, c, backend, _ = world
    backend.services["fn"] = "down"
    assert j(c, "post", "/scenario/passes-review")[0] == 409


def test_responses_never_contain_secret_values(world):
    edge, c, _, _ = world
    edge.calls.add("aws", "secretsmanager:GetSecretValue", {"SecretString": "topsecret", "SecretId": "x"})
    assert "topsecret" not in c.get("/__mock__/calls", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).get_data(as_text=True)
