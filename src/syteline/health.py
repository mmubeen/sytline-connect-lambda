"""
Connectivity check for the SyteLine SOAP endpoint (the `health` action).

Stages, stopping at the first failure:
  1. dns   - resolve the WSDL host
  2. tcp   - open a socket to host:port
  3. wsdl  - download + parse the WSDL (zeep)
  4. token - CreateSessionToken(user, pass, config)  (proves credentials work)

Dry run (event {"dry_run": true} or env SYTELINE_DRY_RUN=true) needs only SYTELINE_WSDL:
no credentials are read or sent and the token stage is skipped. Stages: load, dns, tcp, wsdl.
The wsdl stage is a plain GET: HTTP 401/403 counts as a PASS ("reachable, authentication
required"); 2xx is parsed with zeep; any other status fails.

Opt-in diagnostics (event {"diagnose": true}): adds a separate `diagnostics` section (resolver, ERP name lookup with
retries, connection tests, the same for the function's regional AWS endpoint, plain-language findings). It never changes
`stages` or `ok`, contacts only the ERP host and that AWS endpoint, and sends no data or credentials. Without the flag the
result is exactly as before.

The password is never logged or returned.
"""

import os
import socket
import time
from urllib.parse import urlparse

from . import config as cfgmod
from . import diagnostics, errors, routes


def _stage(name, fn, results):
    start = time.monotonic()
    try:
        detail = fn()
        results.append({"stage": name, "ok": True, "ms": int((time.monotonic() - start) * 1000), "detail": detail})
        return True
    except Exception as exc:  # report, don't raise: we want a structured result
        entry = {
            "stage": name, "ok": False, "ms": int((time.monotonic() - start) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }
        results.append(entry)
        return False


UNENCRYPTED_WARNING = "database connection is UNENCRYPTED: the login and password cross the network unprotected"


def _database_stages(cfg, timeout, results):
    """db_dns / db_tcp / db_login: only a name lookup, a connection and `SELECT 1`; never any credential in the output."""
    from .errors import IntegrationError

    missing = cfgmod.sql_missing(cfg)
    server, port = cfg.get("SQL_SERVER") or "", int(cfg.get("SQL_PORT") or 1433)
    login_timeout = cfgmod.sql_timeouts()[0]

    def need(*keys):
        if any(k in missing for k in keys):
            raise RuntimeError("database not configured: " + ", ".join(k for k in keys if k in missing))

    def db_dns():
        need("SQL_SERVER")
        return sorted({ai[4][0] for ai in socket.getaddrinfo(server, port, type=socket.SOCK_STREAM)})

    def db_tcp():
        with socket.create_connection((server, port), timeout=login_timeout):
            return f"connected to the database port {port}"

    def db_login():
        from .sqlserver import SqlServer

        try:
            with SqlServer(cfg) as db:
                db.ping()
        except IntegrationError as exc:  # DATABASE_NOT_CONFIGURED (key names only)
            raise RuntimeError(exc.err["message"].replace("database settings missing", "database not configured")) from None
        except errors.ErpError as exc:
            text = str(exc)
            low = text.lower()
            if "login" in low and ("failed" in low or "refused" in low):
                raise RuntimeError("login refused") from None
            if "certificate verify failed" in low or "unable to get" in low and "certificate" in low:
                raise RuntimeError("certificate not trusted: the server's certificate is not signed by the "
                                   "authority in SQL_CA_PEM") from None
            if "does not match host name" in low:
                raise RuntimeError("host name does not match the certificate (SYTELINE_SQL_VERIFY_HOSTNAME=false "
                                   "switches only this check off)") from None
            raise RuntimeError(text) from None
        if not db.encrypted:
            return "UNENCRYPTED connection (SYTELINE_SQL_ENCRYPT=false) and login accepted; SELECT 1 answered"
        return "encrypted connection, certificate and login accepted; SELECT 1 answered"

    ok = True
    for name, fn in (("db_dns", db_dns), ("db_tcp", db_tcp), ("db_login", db_login)):
        if not _stage(name, fn, results):
            ok = False
            break  # later database stages depend on earlier ones
    return ok


DRY_RUN_DB_SKIP = "a dry run does not read the secret, which holds the database server name"
SECRET_DB_SKIP = "the credentials secret could not be read"


def _database_rule():
    """Would the health check test the database? (Ignoring a dry run.) Also decides whether diagnostics probe it."""
    return routes.uses_database()


def run_health(event, context=None):
    event = event if isinstance(event, dict) else {}
    diagnose = cfgmod.truthy(event.get("diagnose", False))
    dry_run = cfgmod.truthy(event.get("dry_run", os.environ.get("SYTELINE_DRY_RUN", "false")))
    timeout = cfgmod.timeout()
    verify = cfgmod.verify_tls()

    # Dry run never touches Secrets Manager or credentials.
    try:
        cfg = cfgmod.load_config(skip_secret=dry_run)
    except errors.SecretStoreUnavailable as exc:  # fail promptly, name the credentials store (FR-013)
        out = {"ok": False, "dry_run": dry_run, "error": str(exc), "stages": [],
               "errors": [errors.error(errors.SECRET_STORE_UNREACHABLE, str(exc))]}
        if diagnose:
            out["diagnostics"] = diagnostics.run_diagnostics(cfgmod.load_config(skip_secret=True), timeout)
            if _database_rule():
                out["diagnostics"]["database_skipped"] = SECRET_DB_SKIP
        return out
    required = ("SYTELINE_WSDL",) if dry_run else ("SYTELINE_WSDL", "SYTELINE_USER", "SYTELINE_PASS")
    missing = [k for k in required if not cfg[k]]
    if missing:
        out = {"ok": False, "dry_run": dry_run, "error": f"missing config: {', '.join(missing)}", "stages": []}
        if diagnose:
            out["diagnostics"] = {"skipped": "no ERP address is configured"} if not cfg["SYTELINE_WSDL"] else \
                diagnostics.run_diagnostics(cfg, timeout)
        return out

    warnings = []

    parsed = urlparse(cfg["SYTELINE_WSDL"])
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    results = []
    state = {}

    def load():
        import lxml.etree
        import requests
        import zeep

        return f"zeep {zeep.__version__}, requests {requests.__version__}, lxml {lxml.etree.LXML_VERSION}"

    def dns():
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})

    def tcp():
        with socket.create_connection((host, port), timeout=timeout):
            return f"connected to {host}:{port}"

    def _session():
        import urllib3
        from requests import Session

        if not verify:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        session = Session()
        session.verify = verify
        return session

    def _zeep_client(session):
        from zeep import Client
        from zeep.transports import Transport

        transport = Transport(session=session, cache=None, timeout=timeout, operation_timeout=timeout)
        return Client(cfg["SYTELINE_WSDL"], transport=transport)

    def wsdl():
        session = _session()
        if not dry_run:
            client = _zeep_client(session)
            state["client"] = client
            return "WSDL loaded"
        resp = session.get(cfg["SYTELINE_WSDL"], timeout=timeout)
        if resp.status_code in (401, 403):
            return f"HTTP {resp.status_code}: endpoint reachable, authentication required"
        if resp.ok:
            _check_declared_address(_zeep_client(session))
            return f"HTTP {resp.status_code}: WSDL downloaded and parsed"
        raise RuntimeError(f"unexpected HTTP {resp.status_code}")

    def token():
        tok = state["client"].service.CreateSessionToken(
            cfg["SYTELINE_USER"], cfg["SYTELINE_PASS"], cfg["SYTELINE_CONFIG"]
        )
        if not tok:
            raise RuntimeError("empty session token returned")
        return f"session token received (length {len(str(tok))})"

    plan = [("dns", dns), ("tcp", tcp), ("wsdl", wsdl)]
    plan = [("load", load)] + plan if dry_run else plan + [("token", token)]
    for name, fn in plan:
        if not _stage(name, fn, results):
            break  # later stages depend on earlier ones

    ok = len(results) == len(plan) and all(r["ok"] for r in results)
    # the database is checked on its own: a web-service failure must not hide it. It runs when an operation's route uses it.
    if not dry_run and routes.uses_database():
        db_ok = _database_stages(cfg, timeout, results)
        ok = ok and db_ok
        if not cfgmod.sql_encrypt():  # the explicit option is in use: say so, loudly
            warnings.append(UNENCRYPTED_WARNING)
    try:
        allowed_versions = cfgmod.allowed_config_versions()
    except errors.IntegrationError:
        allowed_versions = []
        warnings.append("ALLOWED_CONFIG_VERSIONS is invalid")
    out = {"ok": ok, "dry_run": dry_run, "host": host, "config": cfg["SYTELINE_CONFIG"], "stages": results,
           "routes": routes.describe(), "allowed_config_versions": allowed_versions}
    if warnings:
        out["warnings"] = warnings
    if diagnose:
        out["diagnostics"] = diagnostics.run_diagnostics(cfg, timeout, database=not dry_run and _database_rule())
        if dry_run and _database_rule():
            out["diagnostics"]["database_skipped"] = DRY_RUN_DB_SKIP
    return out
