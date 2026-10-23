"""
Opt-in connectivity diagnostics for the `health` action (`{"diagnose": true}`, FR-019, SC-008).

Reports, in a separate `diagnostics` section that never changes the health stages:
  * the resolver the function sees (nameservers / search domains from resolv.conf),
  * a name lookup of the ERP host (retried on transient errors, every attempt reported),
  * a TCP connection test to each resolved ERP address and port,
  * the nameserver's own answer: a raw DNS query (A record) sent straight to the function's nameserver(s) for the ERP
    name and the AWS endpoint name, reporting the response code (NOERROR / NXDOMAIN / SERVFAIL / REFUSED) or a timeout.
    It separates "the resolver says no" from "the resolver answers but the function's own lookup fails",
  * the same lookup + connection test for the function's own regional AWS endpoint (the secret store),
  * only when the health check would test the database (`database=True`): the same lookup, nameserver answer and connection
    test for the SQL Server host and port, or `database_skipped` with the reason,
  * `findings`: plain-language conclusions drawn from the combination.

It contacts ONLY the ERP host, that AWS endpoint and (when asked) the database host, sends no data and no credentials, and
reads no secret itself: the database server name and port are the ones the health check already read from the secret. The
database probe is a name lookup and TCP connections only: no login, no TLS handshake, no query.
Probes use the module-level `socket` so tests can replace them.
"""

import errno
import ipaddress
import os
import socket
import struct
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
        out = {"address": address, "port": port, "ok": False, "ms": int((time.monotonic() - start) * 1000), **_err(exc)}
        if ":" in address and getattr(exc, "errno", None) in (errno.EADDRNOTAVAIL, errno.ENETUNREACH, errno.EAFNOSUPPORT):
            out["expected"] = "IPv6 is not available to Lambda functions; this does not matter if an IPv4 address connects"
        return out


# ---- raw DNS: ask the function's own nameserver directly -----------------------------------------------------
RCODES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
DNS_TIMEOUT = 2.0
MAX_NAMESERVERS = 2


def build_query(name, qid):
    """A minimal DNS query for the A record of `name` (recursion desired)."""
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.rstrip(".").split(".")) + b"\x00"
    return struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)


def _skip_name(data, off):
    while True:
        n = data[off]
        if n == 0:
            return off + 1
        if n & 0xC0 == 0xC0:
            return off + 2
        off += 1 + n


def parse_response(data, qid):
    """Parse a DNS response: response code, truncation flag and the A-record addresses."""
    if len(data) < 12:
        raise ValueError("short DNS response")
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if rid != qid:
        raise ValueError("DNS response id does not match the query")
    off = 12
    for _ in range(qd):
        off = _skip_name(data, off) + 4
    answers, cnames = [], 0
    for _ in range(an):
        off = _skip_name(data, off)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        rdata = data[off:off + rdlen]
        off += rdlen
        if rtype == 1 and rdlen == 4:
            answers.append(".".join(str(b) for b in rdata))
        elif rtype == 5:
            cnames += 1
    rcode = flags & 0xF
    return {"rcode": rcode, "rcode_name": RCODES.get(rcode, str(rcode)), "truncated": bool(flags & 0x200),
            "answers": answers, "cnames": cnames}


def query_nameserver(nameserver, name, timeout=DNS_TIMEOUT):
    """Send one UDP query straight to `nameserver`. Never raises; reports what happened."""
    qid = int.from_bytes(os.urandom(2), "big")
    start = time.monotonic()
    result = {"nameserver": nameserver, "name": name}
    sock = None
    try:
        family = socket.AF_INET6 if ":" in nameserver else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(build_query(name, qid), (nameserver, 53))
        data, _ = sock.recvfrom(4096)
        result.update(ok=True, **parse_response(data, qid))
    except socket.timeout:
        result.update(ok=False, error=f"no answer from {nameserver} within {timeout:g}s")
    except (OSError, ValueError) as exc:
        result.update(ok=False, **_err(exc))
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    result["ms"] = int((time.monotonic() - start) * 1000)
    return result


def raw_probe(name, nameservers):
    return [query_nameserver(ns, name) for ns in nameservers[:MAX_NAMESERVERS]]


def probe_host(host, port, timeout):
    """Lookup + connection test to up to MAX_ADDRESSES addresses (IPv4 first)."""
    r = resolve(host, port)
    addrs = sorted(r["addresses"], key=lambda a: ":" in a)[:MAX_ADDRESSES]
    r["port"] = port
    if not r["ok"]:  # a second, independent libc call: does the IPv4-only lookup behave the same?
        try:
            r["gethostbyname"] = {"ok": True, "address": socket.gethostbyname(host)}
        except OSError as exc:
            r["gethostbyname"] = {"ok": False, **_err(exc)}
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
    # No VPC-attachment verdict: the sandbox address is link-local (169.254.x.x) whether or not the function has a VpcConfig.
    return out


def findings(d):
    """Plain-language conclusions. `d` is the diagnostics dict (see run_diagnostics)."""
    erp, aws = d["erp"], d.get("aws")
    out = []
    aws_known = aws is not None
    erp_dns, erp_tcp = erp["ok"], erp.get("connected", False)

    attempts = erp["attempts"]
    raw = (d.get("dns_raw") or {}).get("erp") or []
    raw_answers = [a for r in raw if r.get("ok") and r.get("rcode") == 0 for a in r.get("answers", [])]

    # what the nameserver itself says (raw DNS), compared with what the function's own lookup did
    if not erp_dns and raw:
        rcodes = {r["rcode_name"] for r in raw if r.get("ok")}
        if raw_answers:
            out.append(f"The nameserver answers this name correctly ({', '.join(raw_answers[:4])}) but the function's own "
                       "lookup of it fails: the problem is inside the function's runtime lookup, not in DNS. Compare "
                       "`gethostbyname` below; connecting by the address above would work around it.")
        elif "NXDOMAIN" in rcodes:
            out.append("The nameserver this function uses says the name does not exist (NXDOMAIN): it is not known to that "
                       "resolver. A private plant name needs plant DNS reachable from the function's network (a Route 53 "
                       "Resolver forwarding rule or a private hosted zone).")
        elif rcodes & {"SERVFAIL", "REFUSED"}:
            out.append(f"The nameserver this function uses answered {', '.join(sorted(rcodes & {'SERVFAIL', 'REFUSED'}))}: "
                       "the resolver cannot or will not resolve this name (check the resolver rules and any DNS firewall).")
        elif not any(r.get("ok") for r in raw):
            out.append("No nameserver answered a direct DNS query within the timeout: check the route and security-group "
                       "or network-ACL rules to the resolver (UDP and TCP port 53).")

    # lookup errors that look transient, judged against the rest of the evidence
    if erp_dns and len(attempts) > 1:
        out.append(f"The ERP name resolved only after {len(attempts) - 1} failed attempt(s): the lookup returned errors that "
                   "went away on retry (retrying usually works; check resolver load or the VPC DNS).")
    if not erp_dns and attempts and all(a.get("errno") in TRANSIENT_ERRNOS for a in attempts):
        if aws_known and aws["ok"]:
            out.append(f"Every attempt to look up the ERP name failed with a system error ({attempts[-1].get('error')}) although "
                       "another name resolved through the same resolver in the same run: this is specific to this name, "
                       "not a general resolver outage or overload. See the nameserver's own answer and `gethostbyname`.")
        else:
            out.append(f"Every attempt to look up the ERP name failed with a transient-looking error ({attempts[-1].get('error')}) "
                       "and no other name resolved either: the resolver may be unavailable or overloaded.")

    if not erp_dns and aws_known and not aws["ok"]:
        out.append("Neither the ERP name nor the AWS endpoint name resolved: name resolution does not work at all for "
                   "this function. In a VPC check that DNS support is enabled and which resolver the VPC uses; outside a "
                   "VPC check that the function has outbound network access.")
    elif not erp_dns and aws_known and aws["ok"]:
        out.append("General name resolution works (the AWS endpoint resolved) but the ERP name did not. A private plant name "
                   "needs plant DNS from the function's network (in a VPC: a Route 53 Resolver forwarding rule to the plant "
                   "DNS servers; outside a VPC only publicly resolvable names work).")
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

    if aws_known and not aws.get("connected"):
        out.append("The function cannot reach the credentials store (Secrets Manager), so it cannot read the ERP password"
                   + (" even though the ERP itself is reachable" if erp_dns and erp_tcp else "")
                   + ". When attached to a VPC, add a Secrets Manager endpoint (private DNS on, HTTPS allowed from this "
                   "function's security group) or a NAT route; outside a VPC check the function's outbound access.")

    if erp_dns and erp_tcp and (not aws_known or aws.get("connected")):
        out.append("Name resolution and connections to the ERP"
                   + (" and the AWS endpoint" if aws_known else "") + " all succeeded: if a health stage still fails, "
                   "look at that stage's own error (HTTP status, TLS, login).")
    return out + database_findings(d)


def database_findings(d):
    """Conclusions about the SQL Server host. Empty unless the database was probed."""
    db = d.get("database")
    if not db:
        return []
    aws = d.get("aws")
    raw = (d.get("dns_raw") or {}).get("database") or []
    raw_answers = [a for r in raw if r.get("ok") and r.get("rcode") == 0 for a in r.get("answers", [])]
    out = []
    if not db["ok"]:
        rcodes = {r["rcode_name"] for r in raw if r.get("ok")}
        if raw_answers:
            out.append(f"The nameserver answers the database name correctly ({', '.join(raw_answers[:4])}) but the function's "
                       "own lookup of it fails: the problem is inside the function's runtime lookup, not in DNS.")
        elif "NXDOMAIN" in rcodes:
            out.append("The nameserver this function uses says the database name does not exist (NXDOMAIN): a private plant "
                       "name needs plant DNS reachable from the function's network (a Route 53 Resolver forwarding rule or a "
                       "private hosted zone), or SQL_SERVER in the secret should be an address.")
        elif rcodes & {"SERVFAIL", "REFUSED"}:
            out.append(f"The nameserver answered {', '.join(sorted(rcodes & {'SERVFAIL', 'REFUSED'}))} for the database name: "
                       "the resolver cannot or will not resolve it (check the resolver rules and any DNS firewall).")
        else:
            out.append("The database name did not resolve: check SQL_SERVER in the credentials secret and that plant DNS is "
                       "reachable from the function's network.")
    elif not db.get("connected"):
        where = (f"the database host resolves ({', '.join(db['addresses'][:MAX_ADDRESSES])}) but no connection to port "
                 f"{db['port']} succeeded")
        if aws and aws.get("connected"):
            out.append(f"{where.capitalize()} while the AWS endpoint is reachable: look for a missing route to the database "
                       "(VPN/transit; a function outside a VPC has none to a plant network), a security group or firewall "
                       "blocking that port, or SQL Server not listening on it.")
        else:
            out.append(f"{where.capitalize()}: check routing, the security group on that port and the database server's "
                       "own firewall.")
    else:
        out.append("The database host and port are reachable: if the db_login stage still fails, look at that stage's own "
                   "error (the login, the certificate or the encryption setting).")
    return out


def run_diagnostics(cfg, timeout, env=None, database=False):
    """Probe the ERP host and the regional AWS endpoint, and the database host when `database` is true (the health check
    decides). Sends no data, no credentials; reads no secret itself."""
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
    ns = d["resolver"].get("nameservers", [])
    if ns:  # ask the function's own nameserver(s) directly; sends only a DNS query for these two names
        d["dns_raw"] = {"erp": raw_probe(host, ns), "aws": raw_probe(ep, ns) if ep else None}
    if database:
        server = (cfg.get("SQL_SERVER") or "").strip()
        port = str(cfg.get("SQL_PORT") or "1433").strip()
        if not server:
            d["database_skipped"] = "the secret holds no SQL_SERVER"
        elif not port.isdigit():
            d["database_skipped"] = "SQL_PORT is not a number"
        else:
            d["database"] = probe_host(server, int(port), probe_timeout)
            if ns:
                d["dns_raw"]["database"] = raw_probe(server, ns)
    d["findings"] = findings(d)
    return d
