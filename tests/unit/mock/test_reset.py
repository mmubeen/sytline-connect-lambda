"""T036: a reset returns every part of the world to its starting state (FR-010)."""

import threading

import pytest
from moto.server import ThreadedMotoServer
from werkzeug.test import Client

import render_env
from edge import app as edge_app
from edge import forward, seed
from stubs import StubRuntime

R = render_env.render()


class Counting:
    def __init__(self):
        self.calls = []

    def reset(self):
        self.calls.append("reset")

    def healthy(self):
        return True

    def fault(self, *a):
        self.calls.append(("fault",) + a)

    def clear_faults(self):
        self.calls.append("clear")


@pytest.fixture
def stack(tmp_path):
    moto = ThreadedMotoServer(port=0, verbose=False)
    moto.start()
    host, port = moto.get_host_and_port()
    url = f"http://{host}:{port}"
    sql = Counting()
    w = seed.World(R, url, str(tmp_path), str(tmp_path), sql=sql)
    edge = edge_app.Edge(R, forward.MotoForward(url), StubRuntime(), w)
    yield w, edge, sql, Client(edge)
    moto.stop()


def test_reset_clears_calls_faults_data_and_resets_the_database(stack):
    w, edge, sql, c = stack
    w.reset()
    w.client("dynamodb").put_item(TableName=w.table, Item={"pk": {"S": "x"}})
    edge.calls.add("aws", "x", {})
    edge.faults.add("secrets.unavailable")
    r = c.post("/__mock__/reset", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 200
    assert edge.calls.query() == [] and edge.faults.active() == []
    assert w.client("dynamodb").scan(TableName=w.table)["Items"] == []
    assert "reset" in sql.calls


def test_reset_twice_gives_the_same_starting_state(stack):
    w, *_ = stack
    w.reset()
    first = w.client("dynamodb").describe_table(TableName=w.table)["Table"]["KeySchema"]
    w.reset()
    assert w.client("dynamodb").describe_table(TableName=w.table)["Table"]["KeySchema"] == first
    assert w.client("dynamodb").scan(TableName=w.table)["Items"] == []
