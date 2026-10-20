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
        "SYTELINE_SQL_HOST": os.environ.get("SYTELINE_SQL_HOST", ""),
        "SYTELINE_SQL_PORT": os.environ.get("SYTELINE_SQL_PORT", ""),  # no default: verify with IT
        "SYTELINE_SQL_DB": os.environ.get("SYTELINE_SQL_DB", ""),
        "SYTELINE_SQL_USER": os.environ.get("SYTELINE_SQL_USER", ""),
        "SYTELINE_SQL_PASS": os.environ.get("SYTELINE_SQL_PASS", ""),
    }
    arn = os.environ.get("SYTELINE_SECRET_ARN")
    if arn and not skip_secret:
        import boto3  # bundled in the Lambda runtime

        secret = boto3.client("secretsmanager").get_secret_value(SecretId=arn)
        for k, v in json.loads(secret["SecretString"]).items():
            if k in cfg and v:
                cfg[k] = v
    return cfg


def live_writes_enabled():
    """True only when SYTELINE_LIVE_WRITES is explicitly on. Default: off (FR-016)."""
    return truthy(os.environ.get("SYTELINE_LIVE_WRITES", "false"))


def jobs_table():
    return os.environ.get("JOBS_TABLE", "")


def worker_function_name():
    return os.environ.get("WORKER_FUNCTION_NAME", "")


def sql_configured(cfg):
    return bool(cfg.get("SYTELINE_SQL_HOST") and cfg.get("SYTELINE_SQL_PORT"))


def timeout():
    return float(os.environ.get("SYTELINE_TIMEOUT", "10"))


def verify_tls():
    return os.environ.get("SYTELINE_VERIFY_TLS", "false").lower() == "true"
