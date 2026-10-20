import pytest

from syteline import config


def test_defaults_and_config_name(monkeypatch):
    assert config.load_config()["SYTELINE_CONFIG"] == "CCT"
    monkeypatch.setenv("SYTELINE_CONFIG", "TEST")
    monkeypatch.setenv("SYTELINE_DEFAULT_WHSE", "W1")
    monkeypatch.setenv("SYTELINE_DEFAULT_ORDTYPE", "J")
    cfg = config.load_config()
    assert (cfg["SYTELINE_CONFIG"], cfg["SYTELINE_DEFAULT_WHSE"], cfg["SYTELINE_DEFAULT_ORDTYPE"]) == ("TEST", "W1", "J")


def test_configured_name_reaches_login():
    from syteline.client import SoapIdoClient

    seen = {}

    class Svc:
        def CreateSessionToken(self, u, p, c):
            seen["args"] = (u, p, c)
            return "tok"

    class Z:
        service = Svc()

    cli = SoapIdoClient({"SYTELINE_USER": "u", "SYTELINE_PASS": "p", "SYTELINE_CONFIG": "TEST"}, zeep_client=Z())
    cli.connect()
    assert seen["args"] == ("u", "p", "TEST")


def test_dry_run_skips_secret(monkeypatch):
    monkeypatch.setenv("SYTELINE_SECRET_ARN", "arn:aws:secretsmanager:x")
    config.load_config(skip_secret=True)  # would raise on boto3 call if it tried


def test_live_writes_default_off_and_exact_values(monkeypatch):
    assert config.live_writes_enabled() is False
    for v, expected in (("true", True), ("1", True), ("yes", True), ("false", False), ("", False), ("maybe", False)):
        monkeypatch.setenv("SYTELINE_LIVE_WRITES", v)
        assert config.live_writes_enabled() is expected


def test_no_sql_settings_are_loaded():
    assert not [k for k in config.load_config() if k.startswith("SYTELINE_SQL")]
    assert not hasattr(config, "sql_configured")
