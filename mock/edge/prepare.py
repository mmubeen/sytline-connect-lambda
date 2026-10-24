"""Write the run directory (mock/.run, git-ignored) that the containers start from: the function's environment, the rendered
deployment settings, and generated passwords. Run by `make -C mock up`:  python -m edge.prepare mock/.run

Secrets stay in files with mode 0600 (`passwords.json`, `sql.env`); the function's environment file holds none (the function reads
its passwords from the credentials secret, as in production).
"""

import json
import os
import secrets
import subprocess
import sys
import time
from urllib.parse import urlparse

import render_env
from edge import events, realidp

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


IDP_DIR = os.path.join(os.path.dirname(__file__), "..", ".idp")
_REAL_REQUIRED = ("MOCK_IDP_PROFILE", "MOCK_IDP_STACK", "MOCK_IDP_BUCKET", "MOCK_IDP_TABLE", "MOCK_IDP_DOCUMENT")


def _export_credentials(profile, runner):
    """The profile's temporary credentials as a dict. The values are never printed; a failure names the profile and the AWS CLI's reason."""
    r = runner(["aws", "configure", "export-credentials", "--profile", profile, "--format", "env-no-export"],
               capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"cannot export credentials from the AWS profile {profile!r}: {(r.stderr or '').strip()[:300]}")
    values = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    if not all(values.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")):
        raise SystemExit(f"the AWS profile {profile!r} did not give temporary credentials (a key, a secret and a session token)")
    expiry = realidp._parse_expiry(values.get("AWS_CREDENTIAL_EXPIRATION", ""))
    if expiry is not None and expiry <= time.time():
        raise SystemExit(f"the credentials of the AWS profile {profile!r} have expired: sign in again (aws sso login --profile {profile})")
    return values


def _write_credentials(idp_dir, values):
    os.makedirs(idp_dir, exist_ok=True)
    keys = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_CREDENTIAL_EXPIRATION")
    _write_private(os.path.join(idp_dir, "creds"), "".join(f"{k}={values[k]}\n" for k in keys if values.get(k)))


def refresh_credentials(idp_dir=IDP_DIR, runner=subprocess.run):
    """Re-export the credentials of the profile recorded in mode.json (`make -C mock idp-creds`); nothing else changes."""
    try:
        with open(os.path.join(idp_dir, "mode.json")) as f:
            mode = json.load(f)
    except OSError:
        raise SystemExit("no real-IDP mode is prepared: start with make -C mock up MOCK_IDP=real ...") from None
    if mode.get("mode") != "real":
        raise SystemExit("the environment is not in real-IDP mode")
    _write_credentials(idp_dir, _export_credentials(mode["profile"], runner))


def _real_settings(environ, runner):
    missing = [k for k in _REAL_REQUIRED if not environ.get(k)]
    if missing:
        raise SystemExit("real-IDP mode needs these settings: " + ", ".join(missing))
    try:
        events.build_event(environ["MOCK_IDP_DOCUMENT"])
    except ValueError as exc:
        raise SystemExit(f"MOCK_IDP_DOCUMENT is not usable: {exc}") from None
    profile = environ["MOCK_IDP_PROFILE"]
    creds = _export_credentials(profile, runner)
    who = runner(["aws", "sts", "get-caller-identity", "--profile", profile, "--query", "Account", "--output", "text"],
                 capture_output=True, text=True)
    if who.returncode != 0 or not who.stdout.strip().isdigit():
        raise SystemExit(f"cannot read the account of the AWS profile {profile!r}: {(who.stderr or '').strip()[:300]}")
    region = environ.get("MOCK_IDP_REGION") or "us-gov-east-1"
    partition = "aws-us-gov" if region.startswith("us-gov-") else "aws"
    mode = {"mode": "real", "profile": profile, "stack": environ["MOCK_IDP_STACK"], "bucket": environ["MOCK_IDP_BUCKET"],
            "table": environ["MOCK_IDP_TABLE"], "kms_keys": environ.get("MOCK_IDP_KMS_KEYS", ""), "region": region,
            "partition": partition, "account": who.stdout.strip(), "document": environ["MOCK_IDP_DOCUMENT"],
            "fips": environ.get("MOCK_IDP_FIPS", "true").lower() != "false"}
    return mode, creds


def prepare(run_dir, idp_dir=IDP_DIR, environ=None, runner=subprocess.run):
    environ = os.environ if environ is None else environ
    os.makedirs(run_dir, exist_ok=True)
    mode, creds = ({"mode": "mock"}, None)
    if environ.get("MOCK_IDP", "mock") == "real":
        mode, creds = _real_settings(environ, runner)
    elif environ.get("MOCK_IDP", "mock") != "mock":
        raise SystemExit("MOCK_IDP must be 'mock' (default) or 'real'")
    # how the function reads the IDP result (template parameters IdpFieldMap / IdpDocumentClass), as a deployment would set them
    extra = {name: environ[var] for var, name in (("MOCK_IDP_FIELD_MAP", "IdpFieldMap"), ("MOCK_IDP_DOCUMENT_CLASS", "IdpDocumentClass"))
             if environ.get(var)}
    if extra.get("IdpFieldMap"):
        json.loads(extra["IdpFieldMap"])  # fail here, not in the function, on a malformed value
    if mode["mode"] == "real":
        rendered = render_env.render(account=mode["account"], region=mode["region"], partition=mode["partition"],
                                     params={"IdpStackName": mode["stack"], "IdpOutputBucketName": mode["bucket"],
                                             "IdpTrackingTableName": mode["table"], "IdpKmsKeyArns": mode["kms_keys"], **extra})
        overrides = {**OVERRIDES, "AWS_DEFAULT_REGION": mode["region"], "AWS_REGION": mode["region"], "IDP_REGION": mode["region"]}
    else:
        rendered, overrides = render_env.render(params=extra), OVERRIDES
    env = {**rendered["env"], **overrides,
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
    # the real-IDP directory: credentials only in real mode, and none left over from an earlier real run otherwise
    os.makedirs(idp_dir, exist_ok=True)
    with open(os.path.join(idp_dir, "mode.json"), "w") as f:
        json.dump(mode, f, indent=2)
    stale = os.path.join(idp_dir, "creds")
    if creds:
        _write_credentials(idp_dir, creds)
    elif os.path.exists(stale):
        os.remove(stale)
    return env


if __name__ == "__main__":
    if "--creds-only" in sys.argv:
        refresh_credentials()
    else:
        prepare(next((a for a in sys.argv[1:] if not a.startswith("--")), os.path.join(os.path.dirname(__file__), "..", ".run")))
