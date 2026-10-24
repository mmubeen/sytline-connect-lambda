"""World <-> ERP mock / SQL control wiring, and the ERP call records in the control API."""

import threading

import pytest
from werkzeug.serving import make_server
from werkzeug.test import Client

import render_env
from edge import app as edge_app
from edge import erpcontrol, seed
from erp import app as erp_app
from stubs import StubForward, StubRuntime

R = render_env.render()


@pytest.fixture
def erp():
    mock = erp_app.ErpMock("u", "p")
    srv = make_server("127.0.0.1", 0, mock, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    mock.ctl = erpcontrol.ErpControl(f"http://127.0.0.1:{srv.server_port}")
    yield mock
    srv.shutdown()


class Sql:
    def __init__(self):
        self.events = []

    def reset(self):
        self.events.append("reset")

    def run(self, script):
        self.events.append(("run", script))

    def fault(self, fid, params):
        self.events.append(("fault", fid))

    def clear_faults(self):
        self.events.append("clear")

    def healthy(self):
        return True


def test_erp_faults_and_calls_flow_through_the_world(erp, tmp_path):
    w = seed.World(R, "http://127.0.0.1:9", str(tmp_path), str(tmp_path), erp=erp.ctl)
    w.fault_added({"id": "erp.slow", "params": {"delay_ms": 1}})
    assert erp.faults_active()[0]["id"] == "erp.slow"
    w.fault_cleared(["erp.slow"])
    assert erp.faults_active() == []


def test_sql_faults_and_scenario_script_reach_sql_control(tmp_path):
    sql = Sql()
    w = seed.World(R, "http://127.0.0.1:9", str(tmp_path), str(tmp_path), sql=sql)
    w.fault_added({"id": "sql.slow", "params": {}})
    w.fault_cleared(["sql.slow"])
    assert sql.events == [("fault", "sql.slow"), "clear"]


def test_control_calls_for_erp_come_from_the_erp_mock(erp, tmp_path):
    erp.log.add("erp", "LoadDataSet", {"ido": "SLCos"})
    w = seed.World(R, "http://127.0.0.1:9", str(tmp_path), str(tmp_path), erp=erp.ctl)
    edge = edge_app.Edge(R, StubForward(), StubRuntime(), w)
    edge.calls.add("aws", "dynamodb:PutItem", {})
    c = Client(edge)
    out = c.get("/__mock__/calls?service=erp", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).get_json()
    assert [x["operation"] for x in out] == ["LoadDataSet"]
    both = c.get("/__mock__/calls", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).get_json()
    assert {x["service"] for x in both} == {"erp", "aws"}
