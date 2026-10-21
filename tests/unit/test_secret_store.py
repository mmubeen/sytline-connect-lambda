"""FR-013: an unreachable or unreadable credentials store fails promptly and clearly, never hangs."""

import boto3
import pytest

import lambda_function as lf
from syteline import config, errors, health

HTTPS = "https://erp.example.com/IDORequestService/IDOWebService.asmx?wsdl"


@pytest.fixture
def with_secret(monkeypatch):
    monkeypatch.setenv("SYTELINE_WSDL", HTTPS)
    monkeypatch.setenv("SYTELINE_USER", "u")
    monkeypatch.setenv("SYTELINE_SECRET_ARN", "arn:aws-us-gov:secretsmanager:us-gov-east-1:1:secret:x")


class _Client:
    def __init__(self, exc=None, payload='{"SYTELINE_PASS": "p"}'):
        self.exc, self.payload = exc, payload

    def get_secret_value(self, SecretId):
        if self.exc:
            raise self.exc
        return {"SecretString": self.payload}


def install(monkeypatch, client, seen=None):
    def factory(service, **kw):
        if seen is not None:
            seen.append((service, kw))
        return client
    monkeypatch.setattr(boto3, "client", factory)


def test_the_secret_is_read_with_short_timeouts_and_no_long_retries(with_secret, monkeypatch):
    seen = []
    install(monkeypatch, _Client(), seen)
    assert config.load_config()["SYTELINE_PASS"] == "p"
    cfg = seen[0][1]["config"]
    assert cfg.connect_timeout <= 3 and cfg.read_timeout <= 5 and cfg.retries["max_attempts"] <= 1


@pytest.mark.parametrize("exc", [TimeoutError("connect timed out"), ConnectionError("no route"),
                                 RuntimeError("AccessDeniedException"), KeyError("SecretString")])
def test_any_failure_becomes_one_structured_error_that_names_the_store(with_secret, monkeypatch, exc):
    install(monkeypatch, _Client(exc))
    with pytest.raises(errors.SecretStoreUnavailable) as ei:
        config.load_config()
    assert "Secrets Manager" in str(ei.value) and "endpoint or a NAT route" in str(ei.value)
    assert "arn:aws" not in str(ei.value) and "p" != str(ei.value)


def test_a_malformed_secret_is_the_same_error(with_secret, monkeypatch):
    install(monkeypatch, _Client(payload="not json"))
    with pytest.raises(errors.SecretStoreUnavailable):
        config.load_config()


def test_dry_run_never_touches_the_secret_store(with_secret, monkeypatch):
    install(monkeypatch, _Client(TimeoutError("x")))
    out = health.run_health({"dry_run": True})
    assert "errors" not in out  # unaffected: the dry run reads no secret


def test_health_fails_promptly_with_a_structured_result(with_secret, monkeypatch):
    install(monkeypatch, _Client(TimeoutError("connect timed out")))
    out = health.run_health({})
    assert out["ok"] is False and out["stages"] == [] and out["dry_run"] is False
    assert out["errors"][0]["code"] == "SECRET_STORE_UNREACHABLE" and "Secrets Manager" in out["error"]


def test_health_with_diagnose_shows_whether_the_store_address_is_reachable(with_secret, monkeypatch):
    import socket
    install(monkeypatch, _Client(TimeoutError("connect timed out")))
    monkeypatch.setenv("AWS_REGION", "us-gov-east-1")
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", port))])

    def refuse(addr, timeout=None):
        raise OSError(110, "timed out")

    monkeypatch.setattr(socket, "create_connection", refuse)
    out = health.run_health({"diagnose": True})
    d = out["diagnostics"]
    assert out["errors"][0]["code"] == "SECRET_STORE_UNREACHABLE"
    assert d["aws"]["ok"] is True and d["aws"]["connected"] is False
    assert any("credentials store" in f and "endpoint" in f for f in d["findings"])


@pytest.mark.parametrize("action", ["get_notes", "add_note"])
def test_notes_actions_return_the_same_structured_error(with_secret, monkeypatch, action):
    install(monkeypatch, _Client(TimeoutError("x")))
    monkeypatch.setattr(lf, "_client_factory", None)  # the real client factory reads the secret
    ev = {"action": action, "record_type": "co", "record_key": "1", "text": "t"}
    out = lf.lambda_handler(ev, None)
    assert out["ok"] is False and out["errors"][0]["code"] == "SECRET_STORE_UNREACHABLE"
