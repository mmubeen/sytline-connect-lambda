"""HTTPS guard of the stand-alone deployment (FR-007, SC-007). Off by default, so the VPC version is unchanged."""

import socket

import pytest

import lambda_function as lf
from fakes import FakeSoap
from syteline import client as clientmod
from syteline import config, errors, health
from syteline.client import SoapIdoClient

HTTP = "http://erp.example.com/IDORequestService/IDOWebService.asmx?wsdl"
HTTPS = "https://erp.example.com/IDORequestService/IDOWebService.asmx?wsdl"


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.setenv("SYTELINE_REQUIRE_HTTPS", "true")


@pytest.fixture
def no_traffic(monkeypatch):
    """Fail the test if anything tries to reach the network or build a SOAP client."""
    def boom(*a, **k):
        raise AssertionError("network traffic / client creation before the guard")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    import zeep
    monkeypatch.setattr(zeep, "Client", boom)


def env(monkeypatch, wsdl, **extra):
    monkeypatch.setenv("SYTELINE_WSDL", wsdl)
    monkeypatch.setenv("SYTELINE_USER", "u")
    monkeypatch.setenv("SYTELINE_PASS", "p")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


# ---- config helpers ---------------------------------------------------------------------
def test_guard_is_off_by_default_and_exact(monkeypatch):
    assert config.require_https() is False and config.transport_problem(HTTP) is None
    for v, expected in (("true", True), ("1", True), ("false", False), ("", False)):
        monkeypatch.setenv("SYTELINE_REQUIRE_HTTPS", v)
        assert config.require_https() is expected


def test_problem_only_for_http_without_override(guard, monkeypatch):
    assert config.transport_problem(HTTPS) is None
    assert config.transport_problem(HTTP) == config.INSECURE_MESSAGE
    assert config.transport_problem("file:///etc/x.wsdl") == config.INSECURE_MESSAGE  # not https
    assert config.transport_problem("") is None and config.transport_problem("", must_have_value=True)
    monkeypatch.setenv("SYTELINE_ALLOW_HTTP", "true")
    assert config.transport_problem(HTTP) is None and config.plain_http_in_use(HTTP)
    assert not config.plain_http_in_use(HTTPS)


def test_message_never_echoes_the_address(guard):
    msg = config.transport_problem("http://user:hunter2@erp.example.com/x")
    assert msg == config.INSECURE_MESSAGE and "hunter2" not in msg and "erp.example.com" not in msg


# ---- health -----------------------------------------------------------------------------
@pytest.mark.parametrize("dry", [False, True])
def test_health_refuses_http_before_any_traffic(guard, no_traffic, monkeypatch, dry):
    env(monkeypatch, HTTP)
    out = health.run_health({"dry_run": dry})
    assert out["ok"] is False and out["stages"] == [] and out["dry_run"] is dry
    assert out["errors"][0]["code"] == "INSECURE_TRANSPORT" and "https://" in out["error"]
    assert "erp.example.com" not in str(out)


def test_health_with_the_override_proceeds_and_warns_without_changing_stages(guard, monkeypatch):
    env(monkeypatch, "http://127.0.0.1:9/x?wsdl", SYTELINE_ALLOW_HTTP="true")
    out = health.run_health({"dry_run": True})
    assert out["warnings"] == [config.PLAIN_HTTP_WARNING]
    assert [s["stage"] for s in out["stages"]][:3] == ["load", "dns", "tcp"]  # nothing refused
    assert "errors" not in out


def test_health_https_is_unchanged(guard, monkeypatch):
    env(monkeypatch, "https://127.0.0.1:9/x?wsdl")
    out = health.run_health({"dry_run": True})
    assert "warnings" not in out and "errors" not in out
    assert [s["stage"] for s in out["stages"]][:3] == ["load", "dns", "tcp"]


def test_guard_off_http_works_exactly_as_before(monkeypatch):
    env(monkeypatch, "http://127.0.0.1:9/x?wsdl")
    out = health.run_health({"dry_run": True})
    assert "warnings" not in out and "errors" not in out
    assert [s["stage"] for s in out["stages"]][:3] == ["load", "dns", "tcp"]


class _Svc:
    def __init__(self, address):
        self._binding_options = {"address": address}

    def CreateSessionToken(self, *a):
        raise AssertionError("the password must not be sent to an insecure endpoint")


class _FakeZeepClient:
    def __init__(self, address):
        self.service = _Svc(address)


def _patch_zeep(monkeypatch, address):
    import zeep
    monkeypatch.setattr(zeep, "Client", lambda *a, **k: _FakeZeepClient(address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.0.0.1", 443))])

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: Conn())


def test_https_wsdl_declaring_an_http_service_address_is_refused_before_the_password_is_sent(guard, monkeypatch):
    env(monkeypatch, HTTPS)
    _patch_zeep(monkeypatch, "http://erp.internal:80/IDORequestService/IDOWebService.asmx")
    out = health.run_health({})
    names = [s["stage"] for s in out["stages"]]
    assert names == ["dns", "tcp", "wsdl"] and out["ok"] is False  # stops before the token stage
    assert out["stages"][-1]["code"] == "INSECURE_TRANSPORT"
    assert out["errors"][0]["code"] == "INSECURE_TRANSPORT"
    assert "erp.internal" not in str(out)


def test_unreadable_declared_address_fails_closed(guard, monkeypatch):
    env(monkeypatch, HTTPS)
    _patch_zeep(monkeypatch, "")
    assert health.run_health({})["stages"][-1]["code"] == "INSECURE_TRANSPORT"


def test_https_declared_address_passes_to_the_token_stage(guard, monkeypatch):
    env(monkeypatch, HTTPS)
    _patch_zeep(monkeypatch, "https://erp.example.com/IDORequestService/IDOWebService.asmx")

    class Svc(_Svc):
        def CreateSessionToken(self, *a):
            return "tok"

    import zeep
    monkeypatch.setattr(zeep, "Client", lambda *a, **k: type("C", (), {"service": Svc("https://erp.example.com/x")})())
    out = health.run_health({})
    assert [s["stage"] for s in out["stages"]] == ["dns", "tcp", "wsdl", "token"] and out["ok"]


def test_override_allows_an_http_declared_address(guard, monkeypatch):
    env(monkeypatch, HTTPS, SYTELINE_ALLOW_HTTP="true")
    import zeep

    class Svc(_Svc):
        def CreateSessionToken(self, *a):
            return "tok"

    monkeypatch.setattr(zeep, "Client", lambda *a, **k: type("C", (), {"service": Svc("http://erp.internal/x")})())
    _patch_zeep_net(monkeypatch)
    assert health.run_health({})["ok"] is True


def _patch_zeep_net(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.0.0.1", 443))])

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: Conn())


# ---- notes actions (direct handler) -----------------------------------------------------
def test_notes_refuse_http_before_creating_a_client(guard, monkeypatch):
    env(monkeypatch, HTTP)

    def factory():
        raise AssertionError("a client must not be created")

    monkeypatch.setattr(lf, "_client_factory", factory)
    for ev in ({"action": "get_notes", "record_type": "co", "record_key": "1"},
               {"action": "add_note", "record_type": "co", "record_key": "1", "text": "t"}):
        out = lf.lambda_handler(ev, None)
        assert out["ok"] is False and out["errors"][0]["code"] == "INSECURE_TRANSPORT"


def test_notes_refuse_an_http_address_supplied_by_the_secret(guard, monkeypatch):
    env(monkeypatch, HTTPS)
    import boto3

    class SM:
        def get_secret_value(self, SecretId):
            return {"SecretString": '{"SYTELINE_WSDL": "%s"}' % HTTP}

    monkeypatch.setenv("SYTELINE_SECRET_ARN", "arn:x")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: SM())
    monkeypatch.setattr(lf, "_client_factory", lambda: (_ for _ in ()).throw(AssertionError("no client")))
    out = lf.lambda_handler({"action": "get_notes", "record_type": "co", "record_key": "1"}, None)
    assert out["errors"][0]["code"] == "INSECURE_TRANSPORT"


def test_notes_with_https_and_with_guard_off_are_unchanged(monkeypatch, guard):
    s = FakeSoap()
    s.add_record("SLCos", CoNum="100")
    monkeypatch.setattr(lf, "_client_factory", lambda: s)
    env(monkeypatch, HTTPS)
    assert lf.lambda_handler({"action": "get_notes", "record_type": "co", "record_key": "100"}, None)["ok"] is True
    monkeypatch.delenv("SYTELINE_REQUIRE_HTTPS")
    env(monkeypatch, HTTP)
    assert lf.lambda_handler({"action": "get_notes", "record_type": "co", "record_key": "100"}, None)["ok"] is True


# ---- SoapIdoClient (defense in depth) ---------------------------------------------------
def test_client_refuses_http_before_creating_the_soap_client(guard, monkeypatch):
    import zeep
    monkeypatch.setattr(zeep, "Client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no client")))
    c = SoapIdoClient({"SYTELINE_WSDL": HTTP, "SYTELINE_USER": "u", "SYTELINE_PASS": "p", "SYTELINE_CONFIG": "C"})
    with pytest.raises(errors.InsecureTransport):
        c.connect()
    with pytest.raises(errors.InsecureTransport):
        c.load("SLCos", ["CoNum"])  # not turned into ErpUnreachable


def test_client_refuses_an_http_declared_address(guard, monkeypatch):
    import zeep
    monkeypatch.setattr(zeep, "Client", lambda *a, **k: _FakeZeepClient("http://erp.internal/x"))
    c = SoapIdoClient({"SYTELINE_WSDL": HTTPS, "SYTELINE_USER": "u", "SYTELINE_PASS": "p", "SYTELINE_CONFIG": "C"})
    with pytest.raises(errors.InsecureTransport):
        c.connect()


def test_an_address_that_turns_insecure_after_deployment_is_refused_on_the_next_call(guard, monkeypatch):
    env(monkeypatch, HTTPS)
    s = FakeSoap()
    s.add_record("SLCos", CoNum="100")
    monkeypatch.setattr(lf, "_client_factory", lambda: s)
    assert lf.lambda_handler({"action": "get_notes", "record_type": "co", "record_key": "100"}, None)["ok"]
    monkeypatch.setenv("SYTELINE_WSDL", HTTP)  # configuration changed behind our back
    out = lf.lambda_handler({"action": "get_notes", "record_type": "co", "record_key": "100"}, None)
    assert out["errors"][0]["code"] == "INSECURE_TRANSPORT"
