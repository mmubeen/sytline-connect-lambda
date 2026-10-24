"""T059: real-IDP mode, in one process. A second Moto (behind a thin front for the lookup function) stands in for the developer's
real IDP; the edge re-signs and forwards the function's IDP reads to it; everything else stays on the first Moto and the ERP mock."""

import json
import os
import sys
import threading
import time

import pytest
from moto.server import ThreadedMotoServer
from werkzeug.serving import make_server
from werkzeug.wrappers import Request, Response

import lambda_function
import render_env
from edge import app as edge_app
from edge import erpcontrol, forward, realidp, seed
from erp import app as erp_app
from idp.seeder import IdpSeeder
from inproc import Context, InProcessRuntime

REPO = os.environ.get("IDP_REPO") or os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                                                   "accelerated-intelligent-document-processing-on-aws")
SCENARIOS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "mock", "scenarios")
pytestmark = pytest.mark.skipif(not os.path.isdir(os.path.join(REPO, "lib", "idp_sdk")), reason="IDP checkout not found")
ROUTES = {"NextCoIdo": "SLCos", "NextJobIdo": "SLJobs", "CustomerIdo": "SLCustAddrs", "DefaultsIdo": "SLCos"}
DEV_KEY = "ASIADEVELOPERKEYID"
KNOWN_OUTCOMES = ("refused", "read_only", "reserved", "save_accepted", "ignored", "already_processed")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    for p in ("lib/idp_sdk", "lib/idp_common_pkg"):
        sys.path.insert(0, os.path.join(REPO, p))
    tmp = tmp_path_factory.mktemp("real")
    r = render_env.render(params={**ROUTES, "LiveWrites": "true"})

    # --- the stand-in "real IDP": its own Moto, seeded with the IDP stack and one document, plus the lookup function
    real_moto = ThreadedMotoServer(port=0, verbose=False)
    real_moto.start()
    h, p = real_moto.get_host_and_port()
    real_url = f"http://{h}:{p}"
    seeder = IdpSeeder(r)
    real_world = seed.World(r, real_url, SCENARIOS, str(tmp / "real_run"), idp_seeder=seeder)
    real_world.reset()
    to_moto = forward.MotoForward(real_url)
    seen = []

    @Request.application
    def real_front(request):
        seen.append({"path": request.path, "auth": request.headers.get("Authorization", ""),
                     "token": request.headers.get("X-Amz-Security-Token")})
        if "/functions/" in request.path and request.path.endswith("/invocations"):
            return Response(json.dumps(seeder.lookup(real_world, json.loads(request.get_data() or b"{}"))), 200)
        status, headers, body = to_moto(request.method, request.full_path.rstrip("?"), dict(request.headers),
                                        request.args.to_dict(), request.get_data())
        return Response(body, status, headers)

    real_srv = make_server("127.0.0.1", 0, real_front, threaded=True)
    threading.Thread(target=real_srv.serve_forever, daemon=True).start()
    real_endpoint = f"http://127.0.0.1:{real_srv.server_port}"

    # --- the mock side: first Moto, the ERP mock, the edge in real mode
    moto = ThreadedMotoServer(port=0, verbose=False)
    moto.start()
    h, p = moto.get_host_and_port()
    erp = erp_app.ErpMock("mock-user", "placeholder")
    erp_srv = make_server("127.0.0.1", 0, erp, threaded=True)
    threading.Thread(target=erp_srv.serve_forever, daemon=True).start()
    erp_url = f"http://127.0.0.1:{erp_srv.server_port}"
    r = render_env.render(params={**ROUTES, "LiveWrites": "true",
                                  "SytelineWsdl": erp_url + "/IDORequestService/IDOWebService.asmx?wsdl"})
    creds_path = tmp / "creds"
    creds_path.write_text(f"AWS_ACCESS_KEY_ID={DEV_KEY}\nAWS_SECRET_ACCESS_KEY=dev-secret\nAWS_SESSION_TOKEN=dev-token\n"
                          f"AWS_CREDENTIAL_EXPIRATION={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 3600))}\n")
    creds_path.chmod(0o600)
    creds = realidp.Credentials(str(creds_path))
    P = r["parameters"]
    cfg = realidp.RealConfig(P["IdpStackName"], P["IdpOutputBucketName"], P["IdpTrackingTableName"], "us-gov-east-1", "aws-us-gov",
                             endpoint_for=lambda s: real_endpoint)
    runtime = InProcessRuntime()
    world = seed.World(r, f"http://{h}:{p}", SCENARIOS, str(tmp / "run"), erp=erpcontrol.ErpControl(erp_url),
                       runtime_up=lambda: True, mode={"mode": "real", "document": "mock-pass-001.pdf"},
                       idp_status=creds.status)
    erp.password = world.secret_values()["SYTELINE_PASS"]
    edge = edge_app.Edge(r, forward.MotoForward(f"http://{h}:{p}"), runtime, world, real=(cfg, realidp.RealForward(cfg, creds), creds))
    edge_srv = make_server("127.0.0.1", 0, edge, threaded=True)
    threading.Thread(target=edge_srv.serve_forever, daemon=True).start()
    env = {**r["env"], "AWS_ENDPOINT_URL": f"http://127.0.0.1:{edge_srv.server_port}", "AWS_ACCESS_KEY_ID": "mock",
           "AWS_SECRET_ACCESS_KEY": "mock", "AWS_DEFAULT_REGION": "us-gov-east-1", "AWS_USE_FIPS_ENDPOINT": "false",
           "IDP_REGION": "us-gov-east-1", "AWS_LAMBDA_FUNCTION_NAME": r["function_name"]}
    world.env = env
    world.stand_in = (seeder, real_world)
    yield world, edge, erp, runtime, seen, creds_path
    for srv in (erp_srv, edge_srv, real_srv):
        srv.shutdown()
    moto.stop()
    real_moto.stop()
    for p in ("lib/idp_sdk", "lib/idp_common_pkg"):
        sys.path.remove(os.path.join(REPO, p))


@pytest.fixture(autouse=True)
def function_env(stack, monkeypatch):
    for k, v in stack[0].env.items():
        monkeypatch.setenv(k, v)


def reseed_stand_in(world):
    """Both Motos share one in-process state, so a reset of the mock side also empties the stand-in IDP: put it back."""
    from edge import models

    seeder, real_world = world.stand_in
    seeder.base(real_world)
    seeder.document(real_world, models.load_scenario(os.path.join(SCENARIOS, "passes-review")))


def run(stack, with_erp_data):
    world, edge, erp, runtime, seen, _ = stack
    world.reset()
    reseed_stand_in(world)
    edge.calls.clear()
    edge.faults.clear()
    seen.clear()
    runtime.threads.clear()
    runtime.results.clear()
    if with_erp_data:
        erp.load(json.load(open(os.path.join(SCENARIOS, "passes-review", "erp.json"))))
    event = world.load("real-idp")
    out = lambda_function.lambda_handler(event, Context())
    runtime.wait()
    return out


def test_the_real_document_is_read_through_the_stand_in_and_the_order_goes_to_the_mock_erp(stack):
    world, edge, erp, runtime, seen, _ = stack
    out = run(stack, with_erp_data=True)
    assert out["outcome"] in ("reserved", "save_accepted"), out
    assert not any("exception" in r for r in runtime.results)
    assert any(r.get("CustPo") == "MOCK-PO-0001" for r in erp.rows("SLCos"))
    real = [c for c in edge.calls.query(service="aws") if c["target"] == "real"]
    assert real and {c["action"] for c in real} <= realidp.READ_ONLY
    assert all(c["allowed"] for c in real)


def test_every_real_call_was_signed_with_the_developers_credentials_and_nothing_else_left(stack):
    world, edge, erp, runtime, seen, _ = stack
    run(stack, with_erp_data=True)
    assert seen, "nothing reached the stand-in IDP"
    lookups = [s for s in seen if "/functions/" not in s["path"]]
    assert all(f"Credential={DEV_KEY}/" in s["auth"] and s["token"] == "dev-token" for s in lookups)
    mock_calls = [c for c in edge.calls.query(service="aws") if c["target"] == "mock"]
    assert mock_calls and all(not c["resource"].endswith(("mock-idp-output/",)) for c in mock_calls)
    assert not [c for c in mock_calls if "LookupFunction" in c["resource"]]


def test_with_an_empty_mock_erp_the_outcome_is_reported_not_asserted(stack):
    world, edge, erp, runtime, seen, _ = stack
    out = run(stack, with_erp_data=False)
    assert out["outcome"] in KNOWN_OUTCOMES, out
    # the real document was read, but the (empty) mock database has no such customer: the run stops there, nothing is written
    assert "CUSTOMER_NOT_FOUND" in str(out) and [c for c in erp.calls_since(0) if c["operation"] == "SaveDataSet"] == []


def test_expired_credentials_stop_the_run_with_the_reason_and_no_fallback(stack):
    world, edge, erp, runtime, seen, creds_path = stack
    keep = creds_path.read_text()
    creds_path.write_text(keep.replace(keep.splitlines()[-1], "AWS_CREDENTIAL_EXPIRATION=2020-01-01T00:00:00Z"))
    try:
        assert world.health()["idp"].startswith("credentials expired")
        world.reset()
        reseed_stand_in(world)
        edge.calls.clear()
        seen.clear()
        out = lambda_function.lambda_handler(world.load("real-idp"), Context())
        assert out["outcome"] == "refused" and "IDP_UNAVAILABLE" in [e["code"] for e in out["errors"]]
        assert not seen                              # nothing reached the stand-in IDP
        assert [c for c in edge.calls.query() if c["target"] == "real" and not c["allowed"]]
    finally:
        creds_path.write_text(keep)
