"""The whole intake in one process: event -> IDP read (real SDK) -> customer match -> number reservation -> background save ->
ERP rows, through the edge and Moto, with the ERP mock over HTTP. Skipped without the IDP checkout."""

import json
import os
import sys
import threading

import pytest
from moto.server import ThreadedMotoServer
from werkzeug.serving import make_server

import lambda_function
import render_env
from edge import app as edge_app
from edge import erpcontrol, forward, seed
from erp import app as erp_app
from idp.seeder import IdpSeeder
from inproc import InProcessRuntime

REPO = os.environ.get("IDP_REPO") or os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                                                   "accelerated-intelligent-document-processing-on-aws")
SCENARIOS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "mock", "scenarios")
pytestmark = pytest.mark.skipif(not os.path.isdir(os.path.join(REPO, "lib", "idp_sdk")), reason="IDP checkout not found")

# the web-service routes stand in for the database routes here (no SQL Server in-process)
ROUTES = {"NextCoIdo": "SLCos", "NextJobIdo": "SLJobs", "CustomerIdo": "SLCustAddrs", "DefaultsIdo": "SLCos"}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    for p in ("lib/idp_sdk", "lib/idp_common_pkg"):
        sys.path.insert(0, os.path.join(REPO, p))
    moto = ThreadedMotoServer(port=0, verbose=False)
    moto.start()
    host, port = moto.get_host_and_port()
    moto_url = f"http://{host}:{port}"

    erp = erp_app.ErpMock("mock-user", "placeholder")
    erp_srv = make_server("127.0.0.1", 0, erp, threaded=True)
    threading.Thread(target=erp_srv.serve_forever, daemon=True).start()
    erp_url = f"http://127.0.0.1:{erp_srv.server_port}"

    r = render_env.render(params={**ROUTES, "SytelineWsdl": erp_url + "/IDORequestService/IDOWebService.asmx?wsdl",
                                  "LiveWrites": "true"})
    runtime = InProcessRuntime()
    w = seed.World(r, moto_url, SCENARIOS, str(tmp_path_factory.mktemp("run")), erp=erpcontrol.ErpControl(erp_url),
                   idp_seeder=IdpSeeder(r), runtime_up=lambda: True)
    # the ERP mock's password is the one the world generates for the run
    erp.password = w.secret_values()["SYTELINE_PASS"]
    edge = edge_app.Edge(r, forward.MotoForward(moto_url), runtime, w)
    edge_srv = make_server("127.0.0.1", 0, edge, threaded=True)
    threading.Thread(target=edge_srv.serve_forever, daemon=True).start()

    env = {**r["env"], "AWS_ENDPOINT_URL": f"http://127.0.0.1:{edge_srv.server_port}", "AWS_ACCESS_KEY_ID": "mock",
           "AWS_SECRET_ACCESS_KEY": "mock", "AWS_DEFAULT_REGION": "us-gov-east-1", "AWS_USE_FIPS_ENDPOINT": "false",
           "IDP_REGION": "us-gov-east-1", "AWS_LAMBDA_FUNCTION_NAME": r["function_name"]}
    w.env = env
    yield w, edge, erp, runtime
    erp_srv.shutdown()
    edge_srv.shutdown()
    moto.stop()
    for p in ("lib/idp_sdk", "lib/idp_common_pkg"):
        sys.path.remove(os.path.join(REPO, p))


@pytest.fixture(autouse=True)
def function_env(world, monkeypatch):
    """The suite's autouse fixture clears the SYTELINE_* variables before every test, so the function's settings are applied per test."""
    for k, v in world[0].env.items():
        monkeypatch.setenv(k, v)


def run(world, name):
    w, edge, erp, runtime = world
    w.reset()
    edge.calls.clear()
    event = w.load(name)
    runtime.threads.clear()
    runtime.results.clear()
    first = lambda_function.lambda_handler(event, runtime and __import__("inproc").Context())
    return event, first


def test_a_passing_document_reserves_and_the_background_save_writes_the_order(world):
    w, edge, erp, runtime = world
    _, out = run(world, "passes-review")
    assert out["outcome"] in ("reserved", "save_accepted"), out
    runtime.wait()
    assert not any("exception" in r for r in runtime.results), runtime.results
    ops = [c["operation"] for c in erp.calls_since(0)]
    assert "CallMethod" in ops          # the numbers were reserved
    assert "SaveDataSet" in ops         # and the order was written to the mock ERP only
    assert any(r.get("CustPo") == "MOCK-PO-0001" for r in erp.rows("SLCos"))


def test_the_same_event_twice_is_processed_once(world):
    w, edge, erp, runtime = world
    event, first = run(world, "already-processed")
    runtime.wait()
    writes = len([c for c in erp.calls_since(0) if c["operation"] == "SaveDataSet"])
    second = lambda_function.lambda_handler(event, __import__("inproc").Context())
    runtime.wait()
    assert second["outcome"] == "already_processed"
    assert len([c for c in erp.calls_since(0) if c["operation"] == "SaveDataSet"]) == writes


@pytest.mark.parametrize("scenario,code", [("needs-review", "DOCUMENT_NEEDS_REVIEW"), ("incomplete", "DOCUMENT_INCOMPLETE"),
                                           ("not-found", "DOCUMENT_NOT_READY")])
def test_refused_documents_write_nothing(world, scenario, code):
    w, edge, erp, runtime = world
    _, out = run(world, scenario)
    assert out["outcome"] == "refused" and code in [e["code"] for e in out["errors"]]
    assert [c for c in erp.calls_since(0) if c["operation"] in ("SaveDataSet", "CallMethod")] == []
