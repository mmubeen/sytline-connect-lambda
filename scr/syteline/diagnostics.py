"""
Opt-in connectivity diagnostics for the `health` action (`{"diagnose": true}`, FR-019, SC-008).

Reports, in a separate `diagnostics` section that never changes the health stages:
  * the resolver the function sees (nameservers / search domains from resolv.conf),
  * a name lookup of the ERP host (retried on transient errors, every attempt reported),
  * a TCP connection test to each resolved ERP address and port,
  * the same lookup + connection test for the function's own regional AWS endpoint (the secret store),
  * `findings`: plain-language conclusions drawn from the combination.

It contacts ONLY the ERP host and that AWS endpoint, sends no data and no credentials, and reads no secret.
Probes use the module-level `socket` so tests can replace them.
"""

import errno
import ipaddress
import os
import socket
import time
from urllib.parse import urlparse

MAX_ADDRESSES = 4
RESOLVE_ATTEMPTS = 3
RESOLVE_PAUSE = 0.25
TRANSIENT_ERRNOS = {errno.EBUSY, errno.EAGAIN, errno.EINTR, getattr(socket, "EAI_AGAIN", -3)}


def read_resolv(path="/etc/resolv.conf"):
    out = {"nameservers": [], "search": []}
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    out["nameservers"].append(parts[1])
                elif len(parts) >= 2 and parts[0] in ("search", "domain"):
                    out["search"].extend(parts[1:])
    except OSError as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _err(exc):
    return {"error": f"{type(exc).__name__}: {exc}", "errno": getattr(exc, "errno", None)}


def _transient(exc):
    return getattr(exc, "errno", None) in TRANSIENT_ERRNOS


def resolve(host, port, attempts=RESOLVE_ATTEMPTS, pause=RESOLVE_PAUSE):
    """Look the name up; retry only on errors that look transient. Every attempt is reported."""
    result = {"host": host, "ok": False, "attempts": [], "addresses": []}
    for n in range(attempts):
        start = time.monotonic()
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            result["attempts"].append({"ok": False, "ms": int((time.monotonic() - start) * 1000), **_err(exc)})
            if _transient(exc) and n + 1 < attempts:
                time.sleep(pause)
                continue
            return result
        addrs = []
        for ai in infos:
            a = ai[4][0]
            if a not in addrs:
                addrs.append(a)
        result["attempts"].append({"ok": True, "ms": int((time.monotonic() - start) * 1000)})
        result["ok"], result["addresses"] = True, addrs
        return result
    return result


def connect(address, port, timeout):
    start = time.monotonic()
    try:
        with socket.create_connection((address, port), timeout=timeout):
            pass
        return {"address": address, "port": port, "ok": True, "ms": int((time.monotonic() - start) * 1000)}
    except OSError as exc:
        return {"address": address, "port": port, "ok": False, "ms": int((time.monotonic() - start) * 1000), **_err(exc)}


def probe_host(host, port, timeout):
    """Lookup + connection test to up to MAX_ADDRESSES addresses (IPv4 first)."""
    r = resolve(host, port)
    addrs = sorted(r["addresses"], key=lambda a: ":" in a)[:MAX_ADDRESSES]
    r["port"] = port
    r["connections"] = [connect(a, port, timeout) for a in addrs]
    r["connected"] = any(c["ok"] for c in r["connections"])
    return r


def aws_endpoint(env=None):
    """Hostname of the function's own regional secret-store endpoint, or None if the region is unknown."""
    env = os.environ if env is None else env
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
    if not region:
        return None
    fips = str(env.get("AWS_USE_FIPS_ENDPOINT", "")).lower() in ("1", "true", "yes")
    return f"secretsmanager{'-fips' if fips else ''}.{region}.amazonaws.com"


def _private(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private and not ip.is_link_local


def local_info():
    out = {}
    try:
        out["local_address"] = socket.gethostbyname(socket.gethostname())
    except OSError as exc:
        out["local_address_error"] = f"{type(exc).__name__}: {exc}"
    la = out.get("local_address", "")
    # A Lambda outside a VPC has a link-local 169.254.x.x address; inside a VPC it has a subnet address.
    out["likely_vpc_attached"] = None if not la else not la.startswith("169.254.")
    return out


def findings(d):
    """Plain-language conclusions. `d` is the diagnostics dict (see run_diagnostics)."""
    erp, aws = d["erp"], d.get("aws")
    out = []
    aws_known = aws is not None
    erp_dns, erp_tcp = erp["ok"], erp.get("connected", False)

    # transient lookup errors
    attempts = erp["attempts"]
    if erp_dns and len(attempts) > 1:
        out.append(f"The ERP name resolved only after {len(attempts) - 1} failed attempt(s): the resolver is "
                   "returning transient errors (retrying usually works; check resolver load or the VPC DNS).")
    if not erp_dns and attempts and all(a.get("errno") in TRANSIENT_ERRNOS for a in attempts):
        out.append("Every lookup attempt of the ERP name failed with a transient-looking resolver error "
                   f"({attempts[-1].get('error')}): the resolver is unavailable or overloaded rather than the name being unknown.")

    if not erp_dns and aws_known and not aws["ok"]:
        out.append("Neither the ERP name nor the AWS endpoint name resolved: name resolution does not work at all for "
                   "this function. In a VPC check that DNS support is enabled and which resolver the VPC uses; outside a "
                   "VPC check that the function has outbound network access.")
    elif not erp_dns and aws_known and aws["ok"]:
        out.append("General name resolution works (the AWS endpoint resolved) but the ERP name did not: the name is not "
                   "resolvable from where this function runs. A private plant name needs plant DNS (in a VPC: a Route 53 "
                   "Resolver forwarding rule to the plant DNS servers; in the stand-alone version: use a publicly "
                   "resolvable name).")
    elif not erp_dns and not aws_known:
        out.append("The ERP name did not resolve (the AWS region is unknown, so the comparison lookup was skipped).")

    if erp_dns and not erp_tcp:
        if aws_known and aws.get("connected"):
            out.append(f"The ERP name resolves ({', '.join(erp['addresses'][:MAX_ADDRESSES])}) but no connection to port "
                       f"{erp['port']} succeeded while the AWS endpoint is reachable: look for a missing route to the ERP "
                       "(VPN/transit), a security group or firewall blocking that port, or the ERP being down.")
        elif aws_known and aws["ok"]:
            out.append("The ERP name resolves but neither the ERP nor the AWS endpoint accepts a connection: the function "
                       "has no working outbound path (VPC without routes/endpoints/NAT, or a blocked egress).")
        else:
            out.append("The ERP name resolves but no connection to it succeeded: check routing, security groups and the "
                       "ERP's own firewall.")
        if any(_private(a) for a in erp["addresses"]) and d.get("local", {}).get("likely_vpc_attached") is False:
            out.append("The ERP resolves to a private address but this function is not attached to a VPC, so it cannot "
                       "reach it: use the VPC deployment, or a publicly reachable ERP address.")

    if erp_dns and erp_tcp and aws_known and not aws.get("connected"):
        out.append("The ERP is reachable but the AWS endpoint is not: the function cannot reach Secrets Manager "
                   "(in a VPC: add the Secrets Manager endpoint or a NAT route).")
    if erp_dns and erp_tcp and (not aws_known or aws.get("connected")):
        out.append("Name resolution and connections to the ERP"
                   + (" and the AWS endpoint" if aws_known else "") + " all succeeded: if a health stage still fails, "
                   "look at that stage's own error (HTTP status, TLS, login).")
    return out


def run_diagnostics(cfg, timeout, env=None):
    """Probe the ERP host and the regional AWS endpoint. Sends no data, no credentials; reads no secret."""
    parsed = urlparse(cfg.get("SYTELINE_WSDL", ""))
    host = parsed.hostname
    if not host:
        return {"skipped": "no ERP address is configured"}
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    probe_timeout = min(float(timeout), 5.0)
    d = {"resolver": read_resolv(), "local": local_info(), "erp": probe_host(host, port, probe_timeout)}
    ep = aws_endpoint(env)
    d["aws"] = probe_host(ep, 443, probe_timeout) if ep else None
    if ep is None:
        d["aws_skipped"] = "AWS region is not set in the environment"
    d["findings"] = findings(d)
    return d
