"""The TCP front of the SQL Server: passes bytes through untouched (so TLS is the engine's own), or refuses / delays."""

import socket
import threading
import time

import pytest

from edge import sqlproxy


@pytest.fixture
def echo():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def run():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=lambda c=conn: [c.sendall(d) for d in iter(lambda: c.recv(1024), b"")], daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    yield srv.getsockname()[1]
    srv.close()


@pytest.fixture
def proxy(echo):
    p = sqlproxy.SqlProxy("127.0.0.1", 0, "127.0.0.1", echo)
    p.start()
    yield p
    p.stop()


def talk(port, data=b"hello", timeout=3):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        s.sendall(data)
        return s.recv(1024)
    except ConnectionError:  # the peer closed on us: same thing to a client as an empty answer
        return b""
    finally:
        s.close()


def test_bytes_pass_through_unchanged(proxy):
    assert talk(proxy.port, b"\x16\x03\x01binary") == b"\x16\x03\x01binary"


def test_refuse_mode_closes_the_connection_without_forwarding(proxy):
    proxy.set_mode("refuse")
    assert talk(proxy.port) == b""


def test_delay_mode_waits_before_forwarding(proxy):
    proxy.set_mode("delay", delay_ms=300)
    start = time.monotonic()
    assert talk(proxy.port) == b"hello"
    assert time.monotonic() - start >= 0.3


def test_mode_can_be_cleared(proxy):
    proxy.set_mode("refuse")
    proxy.set_mode(None)
    assert talk(proxy.port) == b"hello"


def test_backend_down_closes_the_client(echo):
    p = sqlproxy.SqlProxy("127.0.0.1", 0, "127.0.0.1", 1)  # nothing listens on port 1
    p.start()
    try:
        assert talk(p.port) == b""
    finally:
        p.stop()
