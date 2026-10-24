"""The client of the function containers: a pool of runtime emulators, because each handles one invocation at a time."""

import json
import threading
import time

import pytest
from werkzeug.serving import make_server
from werkzeug.wrappers import Request, Response

from edge import runtime


def serve(handler):
    srv = make_server("127.0.0.1", 0, handler, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def rie():
    seen = []

    @Request.application
    def app(request):
        event = json.loads(request.get_data())
        seen.append(event)
        if event.get("sleep"):
            time.sleep(event["sleep"])
        if event.get("boom"):
            return Response(json.dumps({"errorMessage": "boom"}), 200, {"X-Amz-Function-Error": "Unhandled"})
        return Response(json.dumps({"echo": event.get("n")}), 200)

    srv = serve(app)
    srv.seen = seen
    srv.url = f"http://127.0.0.1:{srv.server_port}/2015-03-31/functions/function/invocations"
    yield srv
    srv.shutdown()


def test_sync_invoke_returns_status_payload_and_duration(rie):
    out = runtime.Runtime([rie.url]).invoke_sync({"n": 7})
    assert out["status"] == 200 and out["payload"] == {"echo": 7} and out["function_error"] is None
    assert out["duration_ms"] >= 0


def test_function_error_is_reported(rie):
    out = runtime.Runtime([rie.url]).invoke_sync({"boom": 1})
    assert out["function_error"] == "Unhandled" and out["payload"]["errorMessage"] == "boom"


def test_async_invoke_runs_in_the_background(rie):
    rt = runtime.Runtime([rie.url])
    start = time.monotonic()
    rt.invoke_async({"n": 1, "sleep": 0.4})
    assert time.monotonic() - start < 0.2
    deadline = time.monotonic() + 3
    while not rie.seen and time.monotonic() < deadline:
        time.sleep(0.02)
    assert rie.seen


def test_concurrent_invokes_use_different_instances():
    seen = {"a": [], "b": []}

    def make(name):
        @Request.application
        def app(request):
            seen[name].append(1)
            time.sleep(0.3)
            return Response("{}", 200)

        return serve(app)

    a, b = make("a"), make("b")
    rt = runtime.Runtime([f"http://127.0.0.1:{s.server_port}/x" for s in (a, b)])
    t = threading.Thread(target=rt.invoke_sync, args=({},))
    t.start()
    time.sleep(0.05)
    start = time.monotonic()
    rt.invoke_sync({})
    t.join()
    assert time.monotonic() - start < 0.55 and seen["a"] and seen["b"]
    a.shutdown(); b.shutdown()


def test_unreachable_instance_is_a_502_result_not_an_exception():
    out = runtime.Runtime(["http://127.0.0.1:9/x"]).invoke_sync({})
    assert out["status"] == 502 and "could not be reached" in out["payload"]["error"]


def test_healthy_checks_every_instance(rie):
    assert runtime.Runtime([rie.url]).healthy() is True
    assert runtime.Runtime([rie.url, "http://127.0.0.1:9/x"]).healthy() is False
