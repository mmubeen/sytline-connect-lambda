"""Write the run directory (mock/.run, git-ignored) that the containers start from: the function's environment, the rendered
deployment settings, and generated passwords. Run by `make -C mock up`:  python -m edge.prepare mock/.run

Secrets stay in files with mode 0600 (`passwords.json`, `sql.env`); the function's environment file holds none (the function reads
its passwords from the credentials secret, as in production).
"""

import json
import os
import secrets
import sys
from urllib.parse import urlparse

import render_env

MOCK_HOSTS = {"erp", "edge", "sqlserver", "sqlserver-real", "localhost", "127.0.0.1"}
ADDRESS_SETTINGS = ("SYTELINE_WSDL", "AWS_ENDPOINT_URL")
OVERRIDES = {
    "AWS_ENDPOINT_URL": "http://edge:4566",
    "AWS_ACCESS_KEY_ID": "mock", "AWS_SECRET_ACCESS_KEY": "mock",
    "AWS_DEFAULT_REGION": "us-gov-east-1", "AWS_REGION": "us-gov-east-1", "IDP_REGION": "us-gov-east-1",
    # botocore refuses FIPS endpoints together with a custom endpoint URL: a documented fidelity gap
    "AWS_USE_FIPS_ENDPOINT": "false",
    # the mock ERP and database take writes; the address guard (foreign_addresses) keeps them from anywhere else
    "SYTELINE_LIVE_WRITES": "true",
    "PYTHONPATH": "/var/task:/opt/python",
}


def foreign_addresses(env):
    """Names of the address settings that do not point at a mock service (FR-011)."""
    return [name for name in ADDRESS_SETTINGS
            if name in env and (urlparse(env[name]).hostname or "") not in MOCK_HOSTS]


def _write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def prepare(run_dir):
    os.makedirs(run_dir, exist_ok=True)
    rendered = render_env.render()
    env = {**rendered["env"], **OVERRIDES,
           "AWS_LAMBDA_FUNCTION_NAME": rendered["function_name"],
           "AWS_LAMBDA_FUNCTION_TIMEOUT": str(rendered["timeout"]),
           "AWS_LAMBDA_FUNCTION_MEMORY_SIZE": str(rendered["memory"])}
    bad = foreign_addresses(env)
    if bad:
        raise SystemExit("refusing to prepare: these settings do not point at a mock service: " + ", ".join(bad))
    with open(os.path.join(run_dir, "function.env"), "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in sorted(env.items())))
    with open(os.path.join(run_dir, "rendered.json"), "w") as f:
        json.dump(rendered, f, indent=2)
    pw = os.path.join(run_dir, "passwords.json")
    if not os.path.exists(pw):
        _write_private(pw, json.dumps({"SYTELINE_PASS": secrets.token_urlsafe(18), "SQL_PASS": "Aa1!" + secrets.token_urlsafe(18)}))
    sa = os.path.join(run_dir, "sql.env")
    if not os.path.exists(sa):
        _write_private(sa, f"ACCEPT_EULA=Y\nMSSQL_SA_PASSWORD=Sa1!{secrets.token_urlsafe(18)}\n")
    return env


if __name__ == "__main__":
    prepare(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", ".run"))
