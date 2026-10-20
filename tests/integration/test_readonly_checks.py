"""Read-only checks from the real worker network. SKIPPED unless SYTELINE_READONLY_CHECKS=1.

Needs SYTELINE_WSDL and SYTELINE_SQL_HOST/PORT/DB/USER/PASS. Performs DNS, TCP and a read-only
SELECT; it never writes (live writes stay disabled).
"""

import os
import socket
from urllib.parse import urlparse

import pytest

from syteline import config
from syteline.sql import SqlClient

pytestmark = pytest.mark.skipif(os.environ.get("SYTELINE_READONLY_CHECKS") != "1",
                                reason="set SYTELINE_READONLY_CHECKS=1 on the worker network")


def test_live_writes_are_off():
    assert config.live_writes_enabled() is False


def test_dns_and_soap_port():
    u = urlparse(os.environ["SYTELINE_WSDL"])
    port = u.port or (443 if u.scheme == "https" else 80)
    socket.getaddrinfo(u.hostname, port)
    with socket.create_connection((u.hostname, port), timeout=config.timeout()):
        pass


def test_sql_port_and_read_only_select():
    cfg = config.load_config()
    with socket.create_connection((cfg["SYTELINE_SQL_HOST"], int(cfg["SYTELINE_SQL_PORT"])), timeout=config.timeout()):
        pass
    sql = SqlClient(cfg)
    try:
        rows = sql.query("SELECT TOP 1 cust_num FROM custaddr")
        assert isinstance(rows, list)
    finally:
        sql.close()
