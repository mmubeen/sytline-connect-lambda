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
  * `findings`: plain-language conclusions drawn from the combination.

It contacts ONLY the ERP host and that AWS endpoint, sends no data and no credentials, and reads no secret.
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

    attempts = erp["attempts"]
    raw = (d.get("dns_raw") or {}).get("erp") or []
    raw_answers = [a for r in raw if r.get("ok") and r.get("rcode") == 0 for a in r.get("answers", [])]

    # not attached to a VPC: the function cannot use the VPC's resolver at all
    if not erp_dns and d.get("local", {}).get("likely_vpc_attached") is False:
        out.append("This function is NOT attached to a VPC (its address is link-local and its nameserver is Lambda's own, not "
                   "your VPC's), so it cannot use your VPC's DNS. An EC2 instance in your subnet resolves through the VPC "
                   "resolver, which this function does not use. Check that the function has a VpcConfig (subnets and security "
                   "group) and that the stack was deployed in attached mode.")

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
        private_outside_vpc = (any(_private(a) for a in erp["addresses"])
                               and d.get("local", {}).get("likely_vpc_attached") is False)
        if private_outside_vpc:
            # the missing VPC attachment explains the failure; route/firewall advice would mislead
            out.append("The ERP resolves to a private address but this function is not attached to a VPC, so it cannot "
                       "reach it: use the VPC deployment, or a publicly reachable ERP address.")
        elif aws_known and aws.get("connected"):
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
    ns = d["resolver"].get("nameservers", [])
    if ns:  # ask the function's own nameserver(s) directly; sends only a DNS query for these two names
        d["dns_raw"] = {"erp": raw_probe(host, ns), "aws": raw_probe(ep, ns) if ep else None}
    d["findings"] = findings(d)
    return d
