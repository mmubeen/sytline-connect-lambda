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


# ---- database settings (feature 005) -------------------------------------------------------
def test_database_settings_come_from_the_secret_and_missing_ones_are_named(monkeypatch):
    cfg = config.load_config()
    assert cfg["SQL_PORT"] == "1433" and config.sql_missing(cfg) == list(config.SQL_REQUIRED)
    monkeypatch.setenv("SYTELINE_SECRET_ARN", "arn:x")
    secret = {"SQL_SERVER": "db.plant", "SQL_DATABASE": "SL", "SQL_USER": "u", "SQL_PASS": "p", "SQL_PORT": "14330",
              "SQL_CA_PEM": "-----BEGIN CERTIFICATE-----"}
    monkeypatch.setattr(config, "_read_secret", lambda arn: dict(secret, UNKNOWN_KEY="ignored"))
    cfg = config.load_config()
    assert config.sql_missing(cfg) == [] and cfg["SQL_PORT"] == "14330" and "UNKNOWN_KEY" not in cfg
    monkeypatch.setattr(config, "_read_secret", lambda arn: {k: v for k, v in secret.items() if k != "SQL_CA_PEM"})
    assert config.sql_missing(config.load_config()) == ["SQL_CA_PEM"]  # names only, never values


def test_database_tunables_and_the_explicit_hostname_switch(monkeypatch):
    assert config.sql_verify_hostname() is True and config.sql_timeouts() == (5.0, 10.0) and config.site_id() == 10
    assert config.match_settings() == (70.0, 3.0, 1000)
    for v in ("true", "", "no", "0", "FALSE "):
        monkeypatch.setenv("SYTELINE_SQL_VERIFY_HOSTNAME", v)
        assert config.sql_verify_hostname() is (v.strip().lower() != "false")
    monkeypatch.setenv("SYTELINE_SQL_LOGIN_TIMEOUT", "2")
    monkeypatch.setenv("SYTELINE_SQL_TIMEOUT", "30")
    monkeypatch.setenv("SYTELINE_SITE_ID", "20")
    monkeypatch.setenv("SYTELINE_MATCH_MIN_SCORE", "80")
    monkeypatch.setenv("SYTELINE_MATCH_MARGIN", "5")
    monkeypatch.setenv("SYTELINE_MATCH_CAP", "50")
    assert (config.sql_timeouts(), config.site_id(), config.match_settings()) == ((2.0, 30.0), 20, (80.0, 5.0, 50))


# ---- the explicit unencrypted option (feature 005, revision 2026-10-22) ------------------------------------
@pytest.mark.parametrize("value,encrypted", [(None, True), ("", True), ("true", True), ("TRUE", True), ("0", True),
                                              ("no", True), ("off", True), ("false", False), (" FALSE ", False),
                                              ("False", False)])
def test_only_the_exact_value_false_turns_encryption_off(monkeypatch, value, encrypted):
    if value is None:
        monkeypatch.delenv("SYTELINE_SQL_ENCRYPT", raising=False)
    else:
        monkeypatch.setenv("SYTELINE_SQL_ENCRYPT", value)
    assert config.sql_encrypt() is encrypted


def test_the_ca_certificate_is_required_only_while_encrypted(monkeypatch):
    cfg = {"SQL_SERVER": "db", "SQL_DATABASE": "SL", "SQL_USER": "u", "SQL_PASS": "p", "SQL_CA_PEM": ""}
    monkeypatch.delenv("SYTELINE_SQL_ENCRYPT", raising=False)
    assert config.sql_missing(cfg) == ["SQL_CA_PEM"]  # default: encrypted, CA required
    monkeypatch.setenv("SYTELINE_SQL_ENCRYPT", "false")
    assert config.sql_missing(cfg) == []              # explicit unencrypted option: no CA needed
    assert config.sql_missing(dict(cfg, SQL_PASS="")) == ["SQL_PASS"]  # the other keys stay required in both modes
    assert config.sql_missing({}) == ["SQL_SERVER", "SQL_DATABASE", "SQL_USER", "SQL_PASS"]


# ---- event defaults ------------------------------------------------------------------------------------------
def test_event_defaults_use_the_event_date_and_the_configured_warehouse_and_order_type(monkeypatch):
    monkeypatch.setenv("SYTELINE_DEFAULT_WHSE", "MAIN")
    monkeypatch.setenv("SYTELINE_DEFAULT_ORDTYPE", "R")
    assert config.event_defaults("2026-10-22") == {
        "order_date": "2026-10-22", "received_date": "2026-10-22", "warehouse": "MAIN", "order_type": "R", "missing": []}


def test_event_defaults_name_what_is_not_configured(monkeypatch):
    monkeypatch.delenv("SYTELINE_DEFAULT_WHSE", raising=False)
    monkeypatch.setenv("SYTELINE_DEFAULT_ORDTYPE", "  ")  # no longer needed: a job always links to its order (OrdType O)
    assert config.event_defaults("2026-10-22")["missing"] == ["SYTELINE_DEFAULT_WHSE"]


def test_the_background_work_runs_on_this_same_function_unless_another_is_named(monkeypatch):
    monkeypatch.delenv("WORKER_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    assert config.worker_function_name() == ""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "stack-fn")
    assert config.worker_function_name() == "stack-fn"
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "other-fn")
    assert config.worker_function_name() == "other-fn"


# ---- allowed configuration versions (feature 010) ------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [(None, []), ("", []), ("  ", []), ("ChromAlloy-PO", ["ChromAlloy-PO"]),
                                            (" A , B.1 ,c_2", ["A", "B.1", "c_2"])])
def test_the_allowed_versions_are_a_trimmed_list_or_empty(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("ALLOWED_CONFIG_VERSIONS", raising=False)
    else:
        monkeypatch.setenv("ALLOWED_CONFIG_VERSIONS", value)
    assert config.allowed_config_versions() == expected


@pytest.mark.parametrize("value", ["a,,b", "bad value", "x" * 65, "a,", ",a", "a;b"])
def test_a_malformed_list_raises_and_never_allows_everything(monkeypatch, value):
    from syteline import errors
    from syteline.errors import IntegrationError

    monkeypatch.setenv("ALLOWED_CONFIG_VERSIONS", value)
    with pytest.raises(IntegrationError) as exc:
        config.allowed_config_versions()
    assert exc.value.err["code"] == errors.CONFIG_INVALID
