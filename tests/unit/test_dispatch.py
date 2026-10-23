"""The one entry point: API routes, background actions and the default (specs/007, contracts/dispatch.md)."""

import json
import os

import pytest

import lambda_function as lf
from fakes import FakeJobsTable, FakeIdp, FakeLambda, FakeSoap, po_fields
from handlers import routing
from syteline import errors, idp

SAMPLE = json.load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "sample_event.json")))

API_EVENTS = [
    ("POST", "/confirmed-orders", "intake"), ("GET", "/upload-jobs/{id}", "status"),
    ("POST", "/job-reservations", "reserve"), ("POST", "/job-reservations/{id}/save", "save_intake"),
    ("GET", "/job-reservations/{id}", "reservation_status")]


def api_event(method, resource, body=None):
    return {"httpMethod": method, "resource": resource, "path": resource.replace("{id}", "x"),
            "pathParameters": {"id": "x"}, "body": json.dumps(body) if body is not None else None,
            "requestContext": {"identity": {"userArn": "arn:aws-us-gov:iam::1:role/caller"}}}


class Ctx:
    def get_remaining_time_in_millis(self):
        return 300_000


@pytest.mark.parametrize("method,resource,module", API_EVENTS)
def test_each_api_route_reaches_its_handler_with_a_budgeted_context(monkeypatch, method, resource, module):
    seen = []
    handler_module = routing.load(module)
    monkeypatch.setattr(handler_module, "handler", lambda event, ctx: seen.append((event, ctx)) or {"statusCode": 204})
    event = api_event(method, resource)
    assert lf.lambda_handler(event, Ctx()) == {"statusCode": 204}
    assert seen[0][0] is event and isinstance(seen[0][1], routing.Budget)
    assert seen[0][1].get_remaining_time_in_millis() <= routing.BUDGETS[module] * 1000


def test_an_unknown_api_route_is_a_404_and_never_the_intake(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not run")

    monkeypatch.setattr(lf, "_document_event", boom)
    out = lf.lambda_handler(api_event("GET", "/nope"), Ctx())
    assert out["statusCode"] == 404 and json.loads(out["body"])["errors"][0]["code"] == errors.NOT_FOUND


def test_an_explicit_action_beats_an_api_shaped_event(monkeypatch):
    monkeypatch.setenv("SYTELINE_WSDL", "http://127.0.0.1:9/x?wsdl")
    out = lf.lambda_handler({**api_event("POST", "/confirmed-orders"), "action": "health", "dry_run": True}, Ctx())
    assert out["dry_run"] is True and out["stages"][0]["stage"] == "load"


@pytest.mark.parametrize("action,payload,module,entry", [
    ("work_upload", {"upload_id": "u1"}, "worker", "work_upload"),
    ("work_save", {"reservation_id": "r1"}, "worker", "work_save"),
    ("sweep", {}, "sweeper", "sweep")])
def test_the_background_actions_run_their_entry_points_with_the_real_context(monkeypatch, action, payload, module, entry):
    seen = []
    monkeypatch.setattr(routing.load(module), entry, lambda p, ctx: seen.append((p, ctx)) or {"done": True})
    ctx = Ctx()
    out = lf.lambda_handler({"action": action, **payload}, ctx)
    assert out == {"done": True} and seen[0][0]["action"] == action and seen[0][1] is ctx  # no budget wrapper


def test_the_default_action_still_runs_the_idp_intake_for_a_status_change_event(monkeypatch):
    table, fake = FakeJobsTable(), FakeIdp().add("159112.pdf", po_fields(2), document_class="purchase_order")
    from handlers import event_intake as ei
    from syteline import eventstore
    from syteline.reservations import ReservationStore

    store = ReservationStore(table)
    monkeypatch.setattr(idp, "_client_factory", lambda: fake)
    monkeypatch.setattr(ei, "_store_factory", lambda: store)
    monkeypatch.setattr(ei, "_events_factory", lambda: eventstore.EventStore(table))
    monkeypatch.setattr(ei, "_clients_factory", lambda: FakeSoap())
    monkeypatch.setattr(ei, "_lambda_factory", lambda: FakeLambda())
    monkeypatch.setenv("SYTELINE_DEFAULT_WHSE", "MAIN")
    monkeypatch.setenv("SYTELINE_DEFAULT_ORDTYPE", "R")
    out = lf.lambda_handler(SAMPLE, Ctx())
    assert out["action"] == "document_event" and out["document_id"] == "159112.pdf"


def test_a_bare_payload_is_refused_naming_health_and_an_unknown_action_is_refused():
    out = lf.lambda_handler({}, Ctx())
    assert out["errors"][0]["code"] == errors.EVENT_INVALID and '"action": "health"' in out["errors"][0]["message"]
    assert lf.lambda_handler({"action": "nope"}, Ctx())["errors"][0]["code"] == errors.UNKNOWN_ACTION


def test_the_document_event_and_notes_actions_get_a_budgeted_context(monkeypatch):
    seen = []
    from handlers import event_intake as ei

    monkeypatch.setattr(ei, "process", lambda payload, ctx, debug=False: seen.append(ctx) or {"outcome": "ignored"})
    out = lf.lambda_handler({"action": "document_event", **SAMPLE}, Ctx())
    assert out["ok"] is True and isinstance(seen[0], routing.Budget)
    assert seen[0].get_remaining_time_in_millis() <= 55_000


@pytest.mark.parametrize("payload,expected", [
    (lambda e: {**e, "debug": True}, True), (lambda e: {"action": "document_event", "debug": True, "event": e}, True),
    (lambda e: e, False), (lambda e: {**e, "debug": "yes"}, False), (lambda e: {**e, "debug": False}, False)])
def test_debug_true_beside_the_event_is_passed_to_the_intake(monkeypatch, payload, expected):
    from handlers import event_intake as ei

    seen = []
    monkeypatch.setattr(ei, "process", lambda p, ctx, debug=False: seen.append(debug) or {"outcome": "ignored"})
    lf.lambda_handler(payload(SAMPLE), Ctx())
    assert seen == [expected]
