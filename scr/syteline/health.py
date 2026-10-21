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
from . import diagnostics, errors
from .client import config_service_address


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
        if isinstance(exc, errors.InsecureTransport):
            entry["code"] = errors.INSECURE_TRANSPORT
        results.append(entry)
        return False


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
        return out
    required = ("SYTELINE_WSDL",) if dry_run else ("SYTELINE_WSDL", "SYTELINE_USER", "SYTELINE_PASS")
    missing = [k for k in required if not cfg[k]]
    if missing:
        out = {"ok": False, "dry_run": dry_run, "error": f"missing config: {', '.join(missing)}", "stages": []}
        if diagnose:
            out["diagnostics"] = {"skipped": "no ERP address is configured"} if not cfg["SYTELINE_WSDL"] else \
                diagnostics.run_diagnostics(cfg, timeout)
        return out

    # Stand-alone deployment: HTTPS required unless explicitly overridden. Checked after the configuration
    # is resolved (the secret may supply the address) and before ANY traffic to the ERP, dry run included.
    problem = cfgmod.transport_problem(cfg["SYTELINE_WSDL"])
    if problem:
        out = {"ok": False, "dry_run": dry_run, "error": problem,
               "errors": [errors.error(errors.INSECURE_TRANSPORT, problem)], "stages": []}
        if diagnose:  # nothing is sent to the ERP when the HTTPS rule refuses its address
            out["diagnostics"] = {"skipped": "the HTTPS rule refused the ERP address; the ERP was not contacted"}
        return out
    warnings = [cfgmod.PLAIN_HTTP_WARNING] if cfgmod.plain_http_in_use(cfg["SYTELINE_WSDL"]) else []

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

    def _check_declared_address(client):
        """The SOAP calls go to the address declared in the WSDL, not necessarily the WSDL's own address."""
        problem = cfgmod.transport_problem(config_service_address(client), must_have_value=True)
        if problem:
            raise errors.InsecureTransport(problem)

    def wsdl():
        session = _session()
        if not dry_run:
            client = _zeep_client(session)
            _check_declared_address(client)  # before the token stage sends the password
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
    out = {"ok": ok, "dry_run": dry_run, "host": host, "config": cfg["SYTELINE_CONFIG"], "stages": results}
    insecure = [r for r in results if r.get("code") == errors.INSECURE_TRANSPORT]
    if insecure:
        out["errors"] = [errors.error(errors.INSECURE_TRANSPORT, cfgmod.INSECURE_MESSAGE)]
    if warnings:
        out["warnings"] = warnings
    if diagnose:
        out["diagnostics"] = diagnostics.run_diagnostics(cfg, timeout)
    return out
