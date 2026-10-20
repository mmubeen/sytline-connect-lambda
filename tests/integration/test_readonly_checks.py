"""Read-only checks from the real worker network. SKIPPED unless SYTELINE_READONLY_CHECKS=1.

Needs SYTELINE_WSDL, SYTELINE_USER and SYTELINE_PASS. Performs DNS, TCP, a login and one read-only
collection query; it never writes (no insert, no method call) and live writes stay disabled.
"""

import os
import socket
from urllib.parse import urlparse

import pytest

from syteline import config, idos
from syteline.client import SoapIdoClient

pytestmark = pytest.mark.skipif(os.environ.get("SYTELINE_READONLY_CHECKS") != "1",
                                reason="set SYTELINE_READONLY_CHECKS=1 on the worker network")


def test_live_writes_are_off():
    assert config.live_writes_enabled() is False


def test_dns_and_web_service_port():
    u = urlparse(os.environ["SYTELINE_WSDL"])
    port = u.port or (443 if u.scheme == "https" else 80)
    socket.getaddrinfo(u.hostname, port)
    with socket.create_connection((u.hostname, port), timeout=config.timeout()):
        pass


def test_login_and_one_read_only_customer_query():
    client = SoapIdoClient(config.load_config(), timeout=config.timeout(), verify=config.verify_tls())
    rows = client.load(idos.customer_ido(), [idos.CUSTOMER_PROPS["cust_num"]], "", record_cap=1)
    assert isinstance(rows, list)
