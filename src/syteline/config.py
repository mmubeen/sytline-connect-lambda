"""Lambda configuration: environment variables with optional Secrets Manager override."""

import json
import os

def truthy(v):
    return str(v).lower() in ("1", "true", "yes")


def load_config(skip_secret=False):
    cfg = {
        "SYTELINE_WSDL": os.environ.get("SYTELINE_WSDL", ""),
        "SYTELINE_USER": os.environ.get("SYTELINE_USER", ""),
        "SYTELINE_PASS": os.environ.get("SYTELINE_PASS", ""),
        "SYTELINE_CONFIG": os.environ.get("SYTELINE_CONFIG", "") or "CCT",
        "SYTELINE_DEFAULT_WHSE": os.environ.get("SYTELINE_DEFAULT_WHSE", ""),
        "SYTELINE_DEFAULT_ORDTYPE": os.environ.get("SYTELINE_DEFAULT_ORDTYPE", ""),
        # database settings (feature 005): normally supplied by the secret; the environment is for local tests
        "SQL_SERVER": os.environ.get("SQL_SERVER", ""),
        "SQL_DATABASE": os.environ.get("SQL_DATABASE", ""),
        "SQL_USER": os.environ.get("SQL_USER", ""),
        "SQL_PASS": os.environ.get("SQL_PASS", ""),
        "SQL_PORT": os.environ.get("SQL_PORT", "") or "1433",
        "SQL_CA_PEM": os.environ.get("SQL_CA_PEM", ""),
    }
    arn = os.environ.get("SYTELINE_SECRET_ARN")
    if arn and not skip_secret:
        for k, v in _read_secret(arn).items():
            if k in cfg and v:
                cfg[k] = v
    return cfg


SECRET_PATH_HINT = ("could not read the credentials secret from Secrets Manager; check the secret ARN and that this "
                    "function has a network path to Secrets Manager (when attached to a VPC: an endpoint or a NAT route)")


def _read_secret(arn):
    """Read the secret with short timeouts so an unreachable store fails promptly instead of hanging (FR-013)."""
    from .errors import SecretStoreUnavailable

    try:
        import boto3  # bundled in the Lambda runtime
        from botocore.config import Config

        client = boto3.client("secretsmanager", config=Config(
            connect_timeout=3, read_timeout=5, retries={"max_attempts": 1, "mode": "standard"}))
        return json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
    except Exception as exc:  # unreachable, denied, missing, malformed: all the same to the caller
        raise SecretStoreUnavailable(f"{SECRET_PATH_HINT} ({type(exc).__name__})") from None


def live_writes_enabled():
    """True only when SYTELINE_LIVE_WRITES is explicitly on. Default: off (FR-016)."""
    return truthy(os.environ.get("SYTELINE_LIVE_WRITES", "false"))


def require_https():
    """True only when SYTELINE_REQUIRE_HTTPS is exactly on. Set by the stand-alone (no-VPC) deployment only;
    off everywhere else, so the plant deployment keeps working over its HTTP web service."""
    return truthy(os.environ.get("SYTELINE_REQUIRE_HTTPS", "false"))


def allow_http():
    """The explicit plain-HTTP override (credentials then cross the network unencrypted)."""
    return truthy(os.environ.get("SYTELINE_ALLOW_HTTP", "false"))


INSECURE_MESSAGE = ("refusing to contact the ERP: plain HTTP is not allowed here. Use an https:// ERP address, or "
                    "explicitly allow plain HTTP (AllowPlainHttp=true / SYTELINE_ALLOW_HTTP=true), which sends "
                    "credentials unencrypted")
PLAIN_HTTP_WARNING = "plain HTTP override is on: credentials cross the network unencrypted"


def _scheme(url):
    from urllib.parse import urlparse

    return urlparse(str(url or "")).scheme.lower()


def transport_problem(url, must_have_value=False):
    """A message when the guard is on and `url` is not https (and the override is off), else None.

    With must_have_value an empty/unreadable address also counts as a problem (fail closed).
    The message never repeats the address (it could carry credentials or other sensitive parts).
    """
    if not require_https() or allow_http():
        return None
    if not url:
        return INSECURE_MESSAGE if must_have_value else None
    return None if _scheme(url) == "https" else INSECURE_MESSAGE


def plain_http_in_use(url):
    """True when the override is allowing a plain http:// address."""
    return bool(url) and require_https() and allow_http() and _scheme(url) == "http"


def jobs_table():
    return os.environ.get("JOBS_TABLE", "")


def worker_function_name():
    return os.environ.get("WORKER_FUNCTION_NAME", "")


def timeout():
    return float(os.environ.get("SYTELINE_TIMEOUT", "10"))


def verify_tls():
    return os.environ.get("SYTELINE_VERIFY_TLS", "false").lower() == "true"


# -- database settings (feature 005) ---------------------------------------------------------
SQL_REQUIRED = ("SQL_SERVER", "SQL_DATABASE", "SQL_USER", "SQL_PASS", "SQL_CA_PEM")


def db_check_mode():
    """SYTELINE_DB_CHECK as `true`, `false` or `auto` (trimmed, any case; absent, empty or any other value is `auto`).

    Stand-alone health check only: `auto` runs the database stages when the function is attached to a VPC, `true` forces
    them (the deployment refuses it without the attachment), `false` never runs them. The plant deployment never sets it."""
    value = os.environ.get("SYTELINE_DB_CHECK", "auto").strip().lower()
    return value if value in ("true", "false") else "auto"


def attached():
    """True only when SYTELINE_ATTACHED is exactly `true`: the stand-alone function is attached to an existing VPC (set by
    its template), so a private route to the database can exist."""
    return os.environ.get("SYTELINE_ATTACHED", "false").strip().lower() == "true"


def sql_encrypt():
    """False only when SYTELINE_SQL_ENCRYPT is exactly `false` (trimmed, any case): the explicit option to allow an
    UNENCRYPTED database connection. Any other value, including empty or unset, means encrypted (the default)."""
    return os.environ.get("SYTELINE_SQL_ENCRYPT", "true").strip().lower() != "false"


def sql_missing(cfg):
    """Names (never values) of the required database settings that are not set. The CA certificate (`SQL_CA_PEM`) is
    required only while the connection is encrypted; its absence never makes the connection unencrypted."""
    return [k for k in SQL_REQUIRED if (k != "SQL_CA_PEM" or sql_encrypt()) and not str(cfg.get(k) or "").strip()]


def sql_verify_hostname():
    """False only when SYTELINE_SQL_VERIFY_HOSTNAME is exactly `false` (the certificate chain is verified regardless)."""
    return os.environ.get("SYTELINE_SQL_VERIFY_HOSTNAME", "true").strip().lower() != "false"


def sql_timeouts():
    """(login timeout, query timeout) in seconds."""
    return (float(os.environ.get("SYTELINE_SQL_LOGIN_TIMEOUT", "5")), float(os.environ.get("SYTELINE_SQL_TIMEOUT", "10")))


def site_id():
    return int(os.environ.get("SYTELINE_SITE_ID", "10"))


def match_settings():
    """(minimum confident score, ambiguity margin, candidate cap) of the customer search; the demo's defaults."""
    return (float(os.environ.get("SYTELINE_MATCH_MIN_SCORE", "70")), float(os.environ.get("SYTELINE_MATCH_MARGIN", "3")),
            int(os.environ.get("SYTELINE_MATCH_CAP", "1000")))


def event_defaults(event_date):
    """The values an event-started save needs that the IDP document does not hold (specs/006, research R7): both dates
    are the event date; the warehouse and order type are the configured defaults. `missing` names the blank ones."""
    whse = os.environ.get("SYTELINE_DEFAULT_WHSE", "").strip()
    ordtype = os.environ.get("SYTELINE_DEFAULT_ORDTYPE", "").strip()
    missing = [name for name, value in (("SYTELINE_DEFAULT_WHSE", whse), ("SYTELINE_DEFAULT_ORDTYPE", ordtype))
               if not value]
    return {"order_date": event_date, "received_date": event_date, "warehouse": whse, "order_type": ordtype,
            "missing": missing}

