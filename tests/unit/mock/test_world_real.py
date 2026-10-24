"""T058: the world and the control API in real-IDP mode."""

import pytest
from werkzeug.test import Client

import render_env
from edge import app as edge_app
from edge import events, seed
from stubs import StubBackend, StubForward, StubRuntime

R = render_env.render()
MODE = {"mode": "real", "document": "159112.pdf"}


def world(tmp_path, mode=None, status="up"):
    (tmp_path / "scenarios" / "passes-review").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scenarios" / "passes-review" / "scenario.json").write_text("{}")
    return seed.World(R, "http://127.0.0.1:9", str(tmp_path / "scenarios"), str(tmp_path), mode=mode,
                      idp_status=lambda: status)


def test_real_mode_offers_only_the_real_idp_scenario(tmp_path):
    assert world(tmp_path, MODE).scenario_names() == ["real-idp"]
    assert world(tmp_path).scenario_names() == ["passes-review"]


def test_loading_the_real_scenario_returns_the_event_for_the_named_document(tmp_path):
    ev = world(tmp_path, MODE).load("real-idp")
    assert ev == events.build_event("159112.pdf")


def test_health_reports_the_idp_state(tmp_path):
    assert world(tmp_path, MODE, "up").health()["idp"] == "up"
    assert world(tmp_path, MODE, "credentials expired at 2026-10-24T10:00:00Z").health()["idp"].startswith("credentials expired")
    assert world(tmp_path).health()["idp"] == "mock"


def test_mixing_modes_gives_a_reason(tmp_path):
    assert "mock scenarios are not available" in world(tmp_path, MODE).refusal_for("passes-review")
    assert "real-IDP mode is off" in world(tmp_path).refusal_for("real-idp")
    assert world(tmp_path).refusal_for("nonsense") is None


class RealBackend(StubBackend):
    def __init__(self, status="up"):
        super().__init__()
        self.services["idp"] = status

    def scenario_names(self):
        return ["real-idp"]

    def load(self, name):
        return events.build_event("159112.pdf")

    def refusal_for(self, name):
        return "real-IDP mode is on: mock scenarios are not available" if name == "passes-review" else None


def control(backend):
    c = Client(edge_app.Edge(R, StubForward(), StubRuntime(), backend))
    return lambda method, path, **kw: getattr(c, method)("/__mock__" + path, environ_overrides={"REMOTE_ADDR": "127.0.0.1"}, **kw)


def test_the_real_scenario_returns_the_event(tmp_path):
    r = control(RealBackend())("post", "/scenario/real-idp")
    assert r.status_code == 200 and r.get_json()["event"]["detail"]["status"] == "SUCCEEDED"


def test_a_mock_scenario_in_real_mode_is_409_with_the_reason():
    r = control(RealBackend())("post", "/scenario/passes-review")
    assert r.status_code == 409 and "mock scenarios are not available" in r.get_json()["error"]


def test_an_unknown_scenario_is_still_404():
    assert control(RealBackend())("post", "/scenario/nope").status_code == 404


def test_not_ready_until_the_idp_is_up_and_the_reason_is_visible():
    r = control(RealBackend("credentials missing"))("get", "/health").get_json()
    assert r["ready"] is False and r["services"]["idp"] == "credentials missing"
    assert control(RealBackend("up"))("get", "/health").get_json()["ready"] is True


def test_mock_mode_reads_ready_with_idp_mock():
    b = StubBackend()
    b.services["idp"] = "mock"
    assert control(b)("get", "/health").get_json()["ready"] is True


def test_loading_a_scenario_is_refused_while_the_idp_is_not_up():
    r = control(RealBackend("credentials expired at 2026-10-24T10:00:00Z"))("post", "/scenario/real-idp")
    assert r.status_code == 409 and "credentials expired" in str(r.get_json())
