"""T019: every scenario is read back through the function's own IDP code and the real IDP SDK, over the edge and Moto.

Skipped when the IDP accelerator checkout (IDP_REPO, or the sibling directory) is not available.
"""

import os
import sys
import threading

import pytest
from moto.server import ThreadedMotoServer
from werkzeug.serving import make_server

import render_env
from edge import app as edge_app
from edge import forward, seed
from idp.seeder import IdpSeeder
from stubs import StubRuntime
from syteline import errors, idp
from syteline.errors import IntegrationError

REPO = os.environ.get("IDP_REPO") or os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                                                   "accelerated-intelligent-document-processing-on-aws")
SDK = os.path.join(REPO, "lib", "idp_sdk")
COMMON = os.path.join(REPO, "lib", "idp_common_pkg")
SCENARIOS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "mock", "scenarios")
R = render_env.render()

pytestmark = pytest.mark.skipif(not os.path.isdir(SDK), reason="IDP accelerator checkout not found (set IDP_REPO)")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    for p in (SDK, COMMON):
        sys.path.insert(0, p)
    moto = ThreadedMotoServer(port=0, verbose=False)
    moto.start()
    host, port = moto.get_host_and_port()
    moto_url = f"http://{host}:{port}"
    run = tmp_path_factory.mktemp("run")
    world = seed.World(R, moto_url, SCENARIOS, str(run), idp_seeder=IdpSeeder(R))
    edge = edge_app.Edge(R, forward.MotoForward(moto_url), StubRuntime(), world)
    srv = make_server("127.0.0.1", 0, edge, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {"AWS_ENDPOINT_URL": f"http://127.0.0.1:{srv.server_port}", "AWS_ACCESS_KEY_ID": "mock",
           "AWS_SECRET_ACCESS_KEY": "mock", "AWS_DEFAULT_REGION": "us-gov-east-1", "AWS_USE_FIPS_ENDPOINT": "false",
           "IDP_STACK_NAME": R["env"]["IDP_STACK_NAME"], "IDP_REGION": "us-gov-east-1"}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    yield world, edge
    for k, v in saved.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    srv.shutdown()
    moto.stop()
    for p in (SDK, COMMON):
        sys.path.remove(p)


def load(stack, name):
    world, edge = stack
    world.reset()
    edge.calls.clear()
    edge.faults.clear()
    event = world.load(name)
    return event, edge


def doc_id(event):
    import json

    return json.loads(event["detail"]["input"])["document"]["id"]


def test_a_passing_document_is_read_in_full_through_the_real_sdk(stack):
    event, edge = load(stack, "passes-review")
    d = idp.po_details(doc_id(event))
    assert d["details"]["po_number"] == "MOCK-PO-0001" and len(d["details"]["lines"]) == 2
    ops = {c["operation"] for c in edge.calls.query(service="aws")}
    assert {"cloudformation:DescribeStacks", "lambda:Invoke", "s3:GetObject"} <= ops
    assert all(c["allowed"] for c in edge.calls.query(service="aws"))


def test_a_low_confidence_field_needs_review(stack):
    event, _ = load(stack, "needs-review")
    with pytest.raises(IntegrationError) as e:
        idp.po_details(doc_id(event))
    assert e.value.err["code"] == errors.DOCUMENT_NEEDS_REVIEW


def test_missing_fields_are_incomplete(stack):
    event, _ = load(stack, "incomplete")
    with pytest.raises(IntegrationError) as e:
        idp.po_details(doc_id(event))
    assert e.value.err["code"] == errors.DOCUMENT_INCOMPLETE


def test_an_unknown_document_is_not_ready(stack):
    event, _ = load(stack, "not-found")
    with pytest.raises(IntegrationError) as e:
        idp.read_document(doc_id(event))
    assert e.value.err["code"] == errors.DOCUMENT_NOT_READY


def test_a_key_the_role_cannot_use_reproduces_the_production_denial(stack):
    world, edge = stack
    event, _ = load(stack, "passes-review")
    other = R["parameters"]["IdpKmsKeyArns"].replace("0001", "0002")
    world.kms[f"arn:aws-us-gov:s3:::{R['parameters']['IdpOutputBucketName']}/"] = other
    with pytest.raises(IntegrationError) as e:
        idp.read_document(doc_id(event))
    assert e.value.err["code"] == errors.IDP_UNAVAILABLE
    assert other in e.value.debug_detail and "kms:Decrypt" in e.value.debug_detail
    assert any(not c["allowed"] and c["action"] == "s3:GetObject" for c in edge.calls.query(service="aws"))


def test_lookup_denied_fault_reproduces_the_lookup_function_error(stack):
    world, edge = stack
    event, _ = load(stack, "passes-review")
    edge.faults.add("idp.lookup_denied")
    with pytest.raises(IntegrationError) as e:
        idp.read_document(doc_id(event))
    # the SDK swallows the denial and carries on with status "ERROR", so the function sees a validation error: the cause is in
    # the edge's call record (and the SDK's own log line), exactly as in the production log
    assert e.value.err["code"] == errors.IDP_UNAVAILABLE
    denied = [c for c in edge.calls.query(service="aws") if not c["allowed"]]
    assert denied and denied[0]["action"] == "lambda:InvokeFunction" and denied[0]["fault"] == "idp.lookup_denied"


def test_not_found_fault_hides_the_document_and_clearing_restores_it(stack):
    world, edge = stack
    event, _ = load(stack, "passes-review")
    world.fault_added({"id": "idp.not_found"})
    with pytest.raises(IntegrationError) as e:
        idp.read_document(doc_id(event))
    assert e.value.err["code"] == errors.DOCUMENT_NOT_READY
    world.fault_cleared(["idp.not_found"])
    assert idp.po_details(doc_id(event))["details"]["po_number"] == "MOCK-PO-0001"
