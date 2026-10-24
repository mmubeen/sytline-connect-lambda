"""T033/T035: the mock and the deployment agree, and a difference is reported (FR-008, FR-012)."""

import copy
import json
import os

import pytest

import parity
import render_env
from edge import prepare

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


@pytest.fixture
def run(tmp_path):
    prepare.prepare(str(tmp_path), idp_dir=str(tmp_path) + "-idp")
    return str(tmp_path)


def test_a_fresh_run_directory_agrees_with_the_template_and_the_code(run):
    assert parity.check(run) == []


def test_every_action_of_the_function_is_documented_for_the_mock():
    assert parity.undocumented_actions() == []


def test_a_new_environment_setting_in_the_template_is_reported_as_missing_from_a_stale_run(run, template):
    t = copy.deepcopy(template)
    t["Resources"]["DirectFunction"]["Properties"]["Environment"]["Variables"]["NEW_SETTING"] = "x"
    problems = parity.check(run, template=t)
    assert any("NEW_SETTING" in p for p in problems)


def test_a_new_route_is_reported(run, template):
    t = copy.deepcopy(template)
    t["Resources"]["OrderApi"]["Properties"]["DefinitionBody"]["paths"]["/new-route"] = {"get": {}}
    assert any("/new-route" in p for p in parity.check(run, template=t))


def test_a_changed_timeout_or_memory_is_reported(run, template):
    t = copy.deepcopy(template)
    t["Resources"]["DirectFunction"]["Properties"]["Timeout"] = 60
    t["Globals"]["Function"]["MemorySize"] = 512
    problems = parity.check(run, template=t)
    assert any("timeout" in p for p in problems) and any("memory" in p for p in problems)


def test_an_action_missing_from_the_docs_is_reported(monkeypatch):
    import lambda_function

    monkeypatch.setitem(lambda_function.WORK_ACTIONS, "brand_new_action", ("worker", "x"))
    assert parity.undocumented_actions() == ["brand_new_action"]


def test_a_changed_permission_is_reported(run, template):
    t = copy.deepcopy(template)
    t["Resources"]["DirectFunction"]["Properties"]["Policies"].append(
        {"Statement": [{"Effect": "Allow", "Action": "s3:PutObject", "Resource": "*"}]})
    assert any("permission" in p for p in parity.check(run, template=t))


def test_every_route_becomes_a_proxy_event(run):
    from driver import app as driver

    rendered = json.load(open(os.path.join(run, "rendered.json")))
    for method, resource in rendered["routes"]:
        path = resource.replace("{id}", "abc")
        ev = driver.build_event(rendered["routes"], method, path, "{}")
        assert ev["resource"] == resource and ev["httpMethod"] == method
