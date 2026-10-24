"""The control API of the mock environment (contracts/control-api.md): reset, scenarios, faults, call records, invoke."""

import ipaddress
import json

from werkzeug.wrappers import Response

from . import models


def _ready(services):
    return all(v in ("up", "mock") for v in services.values())


def _json(body, status=200):
    return Response(json.dumps(body, default=str), status, {"Content-Type": "application/json"})


def _local(addr):
    try:
        ip = ipaddress.ip_address(addr or "")
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def handle(edge, request):
    if not _local(request.remote_addr):
        return _json({"error": "the control API answers local requests only"}, 403)
    path = request.path[len("/__mock__"):].rstrip("/") or "/"
    method = request.method
    parts = [p for p in path.split("/") if p]

    if (method, path) == ("GET", "/health"):
        services = edge.backend.health()
        return _json({"ready": _ready(services), "services": services})
    if (method, path) == ("POST", "/reset"):
        _reset(edge)
        return _json({"reset": True})
    if (method, path) == ("GET", "/scenarios"):
        return _json(edge.backend.scenario_names())
    if method == "POST" and parts[:1] == ["scenario"] and len(parts) == 2:
        return _scenario(edge, parts[1])
    if (method, path) == ("POST", "/faults"):
        return _add_fault(edge, request)
    if method == "DELETE" and parts[:1] == ["faults"]:
        fault_id = parts[1] if len(parts) == 2 else None
        cleared_ids = [f["id"] for f in edge.faults.active() if fault_id in (None, f["id"])]
        n = edge.faults.clear(fault_id)
        edge.backend.fault_cleared(cleared_ids)
        return _json({"cleared": n})
    if (method, path) == ("GET", "/calls"):
        return _json(_calls(edge, request.args.get("service"), int(request.args.get("since", 0))))
    if (method, path) == ("POST", "/invoke"):
        return _invoke(edge, request)
    if (method, path) == ("POST", "/invoke/api"):
        return _invoke_api(edge, request)
    if (method, path) == ("POST", "/tick/sweep"):
        return _json(edge.runtime.invoke_sync(dict(edge.r["schedule"]["input"]), None))
    return _json({"error": "no such control route"}, 404)


def _calls(edge, service, since):
    """The edge's own records and the ERP mock's, in time order and numbered 1..n in that order (so `since` is one counter)."""
    merged = edge.calls.query() + (edge.backend.erp_calls(0) if hasattr(edge.backend, "erp_calls") else [])
    merged.sort(key=lambda c: c["ts"])
    out = []
    for n, c in enumerate(merged, 1):
        c = {**c, "seq": n}
        if n > since and service in (None, c["service"]):
            out.append(c)
    return out


def _reset(edge):
    edge.calls.clear()
    edge.faults.clear()
    edge.backend.reset()


def _scenario(edge, name):
    if name not in edge.backend.scenario_names():
        reason = edge.backend.refusal_for(name) if hasattr(edge.backend, "refusal_for") else None
        if reason:
            return _json({"error": reason}, 409)
        return _json({"error": f"unknown scenario {name!r}"}, 404)
    health = edge.backend.health()
    if not _ready(health):
        return _json({"error": "environment not ready", "services": health}, 409)
    _reset(edge)
    event = edge.backend.load(name)
    return _json({"loaded": name, "event": event})


def _add_fault(edge, request):
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or not body.get("id"):
        return _json({"error": "body must be {\"id\": ..., \"params\": {...}, \"remaining\": n}"}, 400)
    try:
        fault_id = edge.faults.add(body["id"], body.get("params"), body.get("remaining"))
    except (models.UnknownFault, ValueError) as exc:
        return _json({"error": str(exc)}, 400)
    edge.backend.fault_added(edge.faults.peek(fault_id))
    return _json({"fault_id": fault_id})


def _invoke(edge, request):
    body = request.get_json(silent=True) or {}
    event = body.get("event")
    if not isinstance(event, dict):
        return _json({"error": "event must be a JSON object"}, 400)
    if body.get("mode") == "async":
        edge.runtime.invoke_async(event)
        return _json({"status": 202})
    return _json(edge.runtime.invoke_sync(event, body.get("timeout")))


def _invoke_api(edge, request):
    from driver import app as driver

    body = request.get_json(silent=True) or {}
    try:
        event = driver.build_event(edge.r["routes"], body.get("method", "GET"), body.get("path", "/"),
                                   body.get("body"), body.get("headers"), body.get("query"))
    except LookupError as exc:
        return _json({"status": 404, "payload": {"statusCode": 404, "body": str(exc)}})
    return _json(edge.runtime.invoke_sync(event, body.get("timeout")))
