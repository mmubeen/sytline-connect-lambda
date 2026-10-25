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


def fixed_job_number():
    """JOB_NUMBER (deploy.env): a TEST setting. When set, every reserved line uses this job number and no new one is allocated."""
    return os.environ.get("JOB_NUMBER", "").strip()


def fixed_co_number():
    """CO_NUMBER (deploy.env): a TEST setting. When set, every reservation uses this CO number and none is allocated."""
    return os.environ.get("CO_NUMBER", "").strip()


def live_writes_enabled():
    """True only when SYTELINE_LIVE_WRITES is explicitly on. Default: off (FR-016)."""
    return truthy(os.environ.get("SYTELINE_LIVE_WRITES", "false"))


def jobs_table():
    return os.environ.get("JOBS_TABLE", "")


def worker_function_name():
    """The function that runs the background work: this same Lambda (specs/007), unless one is named explicitly."""
    return os.environ.get("WORKER_FUNCTION_NAME") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")


def timeout():
    return float(os.environ.get("SYTELINE_TIMEOUT", "10"))


def verify_tls():
    return os.environ.get("SYTELINE_VERIFY_TLS", "false").lower() == "true"


# -- database settings (feature 005) ---------------------------------------------------------
SQL_REQUIRED = ("SQL_SERVER", "SQL_DATABASE", "SQL_USER", "SQL_PASS", "SQL_CA_PEM")


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
    are the event date; the warehouse is the configured default. `missing` names the blank ones. The order type is no longer
    needed: a job always links to its customer order (OrdType O, as in the PO OCR demo); `order_type` stays in the result only
    so records and requests that carry it keep their shape."""
    whse = os.environ.get("SYTELINE_DEFAULT_WHSE", "").strip()
    ordtype = os.environ.get("SYTELINE_DEFAULT_ORDTYPE", "").strip()
    missing = [name for name, value in (("SYTELINE_DEFAULT_WHSE", whse),) if not value]
    return {"order_date": event_date, "received_date": event_date, "warehouse": whse, "order_type": ordtype,
            "missing": missing}
