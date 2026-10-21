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
