"""Opt-in connectivity diagnostics of the health check (FR-019, SC-008)."""

import errno
import socket

import pytest

from syteline import diagnostics as dg
from syteline import health

ERP = "cctsl01.cgtc.com"
AWS = "secretsmanager-fips.us-gov-east-1.amazonaws.com"


class FakeNet:
    """Replaces getaddrinfo / create_connection. dns: {host: [addresses] | [exception, ...]} (a list is consumed per call);
    tcp: {(address, port): True | exception}."""

    def __init__(self, dns, tcp=None):
        self.dns = {h: list(v) for h, v in dns.items()}
        self.tcp = tcp or {}
        self.lookups, self.connections = [], []

    def getaddrinfo(self, host, port, type=0, **k):
        self.lookups.append(host)
        seq = self.dns.get(host)
        if seq is None:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        item = seq[0] if len(seq) == 1 else seq.pop(0)  # the last entry repeats
        if isinstance(item, Exception):
            raise item
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in item] \
            if isinstance(item, list) else [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (item, port))]

    def create_connection(self, addr, timeout=None):
        self.connections.append(addr)
        r = self.tcp.get(addr, OSError(errno.ETIMEDOUT, "timed out"))
        if r is not True:
            raise r

        class C:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

        return C()


@pytest.fixture
def net(monkeypatch):
    def install(dns, tcp=None, local="169.254.76.1"):
        n = FakeNet(dns, tcp)
        monkeypatch.setattr(socket, "getaddrinfo", n.getaddrinfo)
        monkeypatch.setattr(socket, "create_connection", n.create_connection)
        monkeypatch.setattr(socket, "gethostname", lambda: "host")
        monkeypatch.setattr(socket, "gethostbyname", lambda h: local)
        monkeypatch.setattr(dg, "RESOLVE_PAUSE", 0)
        monkeypatch.setenv("AWS_REGION", "us-gov-east-1")
        monkeypatch.setenv("AWS_USE_FIPS_ENDPOINT", "true")
        return n
    return install


def busy():
    return OSError(errno.EBUSY, "Device or resource busy")


# ---- pieces -----------------------------------------------------------------------------
def test_read_resolv(tmp_path):
    f = tmp_path / "resolv.conf"
    f.write_text("# c\nnameserver 10.0.0.2\nnameserver 10.0.0.3\nsearch corp.example internal\noptions x\n")
    assert dg.read_resolv(str(f)) == {"nameservers": ["10.0.0.2", "10.0.0.3"], "search": ["corp.example", "internal"]}
    assert "error" in dg.read_resolv(str(tmp_path / "missing"))


def test_resolve_success_is_a_single_attempt(net):
    n = net({ERP: ["10.9.1.5"]})
    r = dg.resolve(ERP, 80)
    assert r["ok"] and r["addresses"] == ["10.9.1.5"] and len(r["attempts"]) == 1 and n.lookups == [ERP]


def test_transient_errors_are_retried_and_every_attempt_is_reported(net):
    net({ERP: [busy(), busy(), ["10.9.1.5"]]})
    r = dg.resolve(ERP, 80)
    assert r["ok"] and [a["ok"] for a in r["attempts"]] == [False, False, True]
    assert r["attempts"][0]["errno"] == errno.EBUSY and "Device or resource busy" in r["attempts"][0]["error"]


def test_a_persistent_transient_error_gives_up_after_three_attempts(net):
    n = net({ERP: [busy()]})
    r = dg.resolve(ERP, 80)
    assert not r["ok"] and len(r["attempts"]) == 3 and n.lookups == [ERP] * 3


def test_an_unknown_name_is_not_retried(net):
    n = net({})
    r = dg.resolve(ERP, 80)
    assert not r["ok"] and len(r["attempts"]) == 1 and n.lookups == [ERP]


def test_connect_reports_success_and_the_error(net):
    net({}, {("10.9.1.5", 80): True})
    assert dg.connect("10.9.1.5", 80, 1)["ok"] is True
    bad = dg.connect("10.9.1.6", 80, 1)
    assert bad["ok"] is False and bad["errno"] == errno.ETIMEDOUT and "timed out" in bad["error"]


def test_probe_host_caps_addresses_and_tries_ipv4_first(net):
    addrs = ["2001:db8::1", "10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.5"]
    n = net({ERP: [addrs]}, {(a, 80): True for a in addrs})
    r = dg.probe_host(ERP, 80, 1)
    assert len(r["connections"]) == dg.MAX_ADDRESSES and r["connected"]
    assert all(":" not in c["address"] for c in r["connections"]) and len(n.connections) == dg.MAX_ADDRESSES


def test_aws_endpoint_name():
    assert dg.aws_endpoint({"AWS_REGION": "us-gov-east-1"}) == "secretsmanager.us-gov-east-1.amazonaws.com"
    assert dg.aws_endpoint({"AWS_REGION": "us-gov-east-1", "AWS_USE_FIPS_ENDPOINT": "true"}) == AWS
    assert dg.aws_endpoint({"AWS_DEFAULT_REGION": "us-gov-west-1"}) == "secretsmanager.us-gov-west-1.amazonaws.com"
    assert dg.aws_endpoint({}) is None


def test_local_info_makes_no_vpc_attachment_claim(net):
    for addr in ("169.254.76.1", "10.50.0.17"):
        net({}, local=addr)
        assert "likely_vpc_attached" not in dg.local_info()


# ---- findings: what the combination means ------------------------------------------------
def diag(erp_dns, erp_tcp, aws_dns, aws_tcp, erp_addr="10.9.1.5", attempts=None):
    erp = {"host": ERP, "port": 80, "ok": erp_dns, "addresses": [erp_addr] if erp_dns else [],
           "attempts": attempts or [{"ok": erp_dns}], "connected": erp_tcp}
    aws = {"host": AWS, "port": 443, "ok": aws_dns, "addresses": ["10.0.0.9"] if aws_dns else [],
           "attempts": [{"ok": aws_dns}], "connected": aws_tcp}
    return {"erp": erp, "aws": aws, "local": {}}


def text(d):
    return " ".join(dg.findings(d))


def test_nothing_resolves_means_dns_is_broken_for_the_function():
    assert "name resolution does not work at all" in text(diag(False, False, False, False))


def test_only_the_erp_name_failing_points_at_plant_dns():
    t = text(diag(False, False, True, True))
    assert "General name resolution works" in t and "Route 53 Resolver forwarding rule" in t


def test_the_reported_case_busy_on_every_attempt_with_aws_resolving():
    d = diag(False, False, True, True, attempts=[{"ok": False, "errno": errno.EBUSY, "error": "OSError: [Errno 16]"}] * 3)
    t = text(d)
    assert "failed with a system error" in t and "specific to this name" in t and "General name resolution works" in t
    assert "overloaded" not in t  # the resolver demonstrably served another lookup, so it is not "overloaded"


def test_no_other_name_resolving_keeps_the_resolver_unavailable_wording():
    d = diag(False, False, False, False, attempts=[{"ok": False, "errno": errno.EBUSY, "error": "OSError: [Errno 16]"}] * 3)
    assert "may be unavailable or overloaded" in text(d)


def test_resolving_after_retries_is_called_out_as_transient():
    d = diag(True, True, True, True, attempts=[{"ok": False}, {"ok": False}, {"ok": True}])
    assert "only after 2 failed attempt" in text(d)


def test_erp_resolves_but_does_not_connect_while_aws_does():
    t = text(diag(True, False, True, True))
    assert "missing route to the ERP" in t and "security group or firewall" in t


def test_no_outbound_path_when_neither_connects():
    assert "no working outbound path" in text(diag(True, False, True, False))


def test_a_private_erp_that_does_not_connect_never_claims_the_function_is_outside_a_vpc():
    # a link-local sandbox address says nothing about VpcConfig, so no finding may assert "not attached"
    t = text(diag(True, False, True, True, erp_addr="10.9.1.5"))
    assert "missing route to the ERP" in t and "not attached" not in t.lower()


def test_erp_ok_but_the_secret_store_unreachable():
    t = text(diag(True, True, True, False))
    assert "cannot reach the credentials store (Secrets Manager)" in t and "even though the ERP itself is reachable" in t
    assert "Secrets Manager endpoint" in t and "NAT route" in t


def test_secret_store_unreachable_is_reported_whatever_the_erp_result():
    assert "credentials store" in text(diag(False, False, True, False))
    assert "credentials store" not in text(diag(True, True, True, True))


def test_everything_ok_points_at_the_failing_stage():
    assert "look at that stage's own error" in text(diag(True, True, True, True))


# ---- health integration -----------------------------------------------------------------
@pytest.fixture
def cfg_env(monkeypatch):
    monkeypatch.setenv("SYTELINE_WSDL", f"http://{ERP}/IDORequestService/IDOWebService.asmx?wsdl")
    monkeypatch.setenv("SYTELINE_USER", "u")
    monkeypatch.setenv("SYTELINE_PASS", "p")


def stable(out):
    out = {k: v for k, v in out.items() if k != "diagnostics"}
    out["stages"] = [{k: v for k, v in s.items() if k != "ms"} for s in out["stages"]]
    return out


def test_diagnose_adds_a_section_without_changing_stages_or_ok(net, cfg_env):
    net({ERP: [busy()], AWS: ["10.0.0.9"]}, {("10.0.0.9", 443): True})
    plain = health.run_health({"dry_run": True})
    with_diag = health.run_health({"dry_run": True, "diagnose": True})
    assert "diagnostics" not in plain and "diagnostics" in with_diag
    assert stable(with_diag) == stable(plain)
    d = with_diag["diagnostics"]
    assert d["erp"]["ok"] is False and len(d["erp"]["attempts"]) == 3 and d["aws"]["connected"] is True
    assert d["aws"]["host"] == AWS and any("specific to this name" in f for f in d["findings"])
    assert {"resolver", "local", "erp", "aws", "findings"} <= set(d)


def test_diagnostics_also_run_when_everything_passes(net, cfg_env, monkeypatch):
    net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"]}, {("10.9.1.5", 80): True, ("10.0.0.9", 443): True})
    out = health.run_health({"dry_run": True, "diagnose": True})
    assert out["diagnostics"]["erp"]["connected"] and "all succeeded" in " ".join(out["diagnostics"]["findings"])


def test_no_flag_means_no_diagnostics_and_no_extra_probe(net, cfg_env):
    n = net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"]}, {("10.9.1.5", 80): True})
    out = health.run_health({"dry_run": True})
    assert "diagnostics" not in out and AWS not in n.lookups


def test_only_the_erp_host_and_the_aws_endpoint_are_ever_contacted(net, cfg_env):
    n = net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"]}, {("10.9.1.5", 80): True, ("10.0.0.9", 443): True})
    health.run_health({"diagnose": True})
    assert set(n.lookups) <= {ERP, AWS}
    # the health check's own tcp stage connects by name; the diagnostics only to the resolved ERP and AWS addresses
    assert set(n.connections) <= {("10.9.1.5", 80), ("10.0.0.9", 443), (ERP, 80)}


def test_no_secret_is_read_and_no_credentials_are_used_for_the_diagnostics(net, cfg_env, monkeypatch):
    net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"]}, {("10.9.1.5", 80): True, ("10.0.0.9", 443): True})
    import boto3
    monkeypatch.setenv("SYTELINE_SECRET_ARN", "arn:x")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("secret must not be read")))
    out = health.run_health({"dry_run": True, "diagnose": True})
    assert out["diagnostics"]["erp"]["connected"] is True


def test_missing_erp_address_is_skipped_with_a_reason(net, monkeypatch):
    net({})
    out = health.run_health({"diagnose": True, "dry_run": True})
    assert out["ok"] is False and out["diagnostics"] == {"skipped": "no ERP address is configured"}


def test_missing_region_skips_only_the_aws_comparison(net, cfg_env, monkeypatch):
    net({ERP: ["10.9.1.5"]}, {("10.9.1.5", 80): True})
    monkeypatch.delenv("AWS_REGION")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    d = health.run_health({"dry_run": True, "diagnose": True})["diagnostics"]
    assert d["aws"] is None and "region" in d["aws_skipped"] and d["erp"]["connected"]


def test_event_flag_parsing(net, cfg_env):
    net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"]}, {})
    for v, expected in ((True, True), ("true", True), ("1", True), (False, False), ("false", False), (None, False)):
        out = health.run_health({"dry_run": True, "diagnose": v})
        assert ("diagnostics" in out) is expected


# ---- raw DNS: ask the function's own nameserver ------------------------------------------
import struct


def make_response(query, rcode=0, ips=(), truncated=False, mismatch=False):
    qid = struct.unpack(">H", query[:2])[0] ^ (1 if mismatch else 0)
    flags = 0x8180 | rcode | (0x200 if truncated else 0)
    question = query[12:]
    body = b"".join(b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes(int(x) for x in ip.split(".")) for ip in ips)
    return struct.pack(">HHHHHH", qid, flags, 1, len(ips), 0, 0) + question + body


def qname_of(query):
    labels, off = [], 12
    while query[off]:
        n = query[off]
        labels.append(query[off + 1:off + 1 + n].decode())
        off += 1 + n
    return ".".join(labels)


class FakeUdp:
    """Scripted nameserver: rules = {name: ("answer", rcode, ips) | ("timeout",) | ("oserror", errno)}."""
    instances = []

    def __init__(self, rules, family, kind):
        self.rules, self.family, self.kind = rules, family, kind
        self.sent, self._reply, self.closed = [], None, False
        FakeUdp.instances.append(self)

    def settimeout(self, t):
        self.timeout = t

    def sendto(self, data, addr):
        self.sent.append((addr, qname_of(data)))
        rule = self.rules.get(qname_of(data), ("timeout",))
        if rule[0] == "oserror":
            raise OSError(rule[1], "network error")
        self._reply = rule if rule[0] == "answer" else None
        self._query = data

    def recvfrom(self, n):
        if self._reply is None:
            raise socket.timeout()
        kind, rcode, ips, *extra = self._reply
        return make_response(self._query, rcode, ips, truncated=bool(extra and extra[0]), mismatch=bool(extra and len(extra) > 1)), ("ns", 53)

    def close(self):
        self.closed = True


@pytest.fixture
def udp(monkeypatch):
    def install(rules):
        FakeUdp.instances.clear()
        monkeypatch.setattr(socket, "socket", lambda family=socket.AF_INET, kind=socket.SOCK_DGRAM: FakeUdp(rules, family, kind))
    return install


def test_query_packet_is_a_valid_a_record_query():
    q = dg.build_query("cctsl01.cgtc.com", 0x1234)
    assert q[:2] == b"\x12\x34" and struct.unpack(">H", q[4:6])[0] == 1 and qname_of(q) == "cctsl01.cgtc.com"
    assert q[-4:] == struct.pack(">HH", 1, 1)


def test_noerror_answer_is_parsed_including_compressed_names(udp):
    udp({ERP: ("answer", 0, ["10.9.1.5", "10.9.1.6"])})
    r = dg.query_nameserver("169.254.100.5", ERP)
    assert r["ok"] and r["rcode_name"] == "NOERROR" and r["answers"] == ["10.9.1.5", "10.9.1.6"] and r["truncated"] is False
    assert FakeUdp.instances[0].sent == [(("169.254.100.5", 53), ERP)] and FakeUdp.instances[0].closed


@pytest.mark.parametrize("rcode,name", [(3, "NXDOMAIN"), (2, "SERVFAIL"), (5, "REFUSED")])
def test_error_response_codes_are_named(udp, rcode, name):
    udp({ERP: ("answer", rcode, [])})
    r = dg.query_nameserver("10.0.0.2", ERP)
    assert r["ok"] and r["rcode_name"] == name and r["answers"] == []


def test_timeout_and_network_errors_are_reported_not_raised(udp):
    udp({})
    r = dg.query_nameserver("10.0.0.2", ERP, timeout=2)
    assert r["ok"] is False and "no answer from 10.0.0.2 within 2s" in r["error"]
    udp({ERP: ("oserror", errno.ENETUNREACH)})
    r = dg.query_nameserver("10.0.0.2", ERP)
    assert r["ok"] is False and r["errno"] == errno.ENETUNREACH


def test_a_response_with_the_wrong_id_is_rejected(udp):
    udp({ERP: ("answer", 0, ["10.9.1.5"], False, True)})
    r = dg.query_nameserver("10.0.0.2", ERP)
    assert r["ok"] is False and "id does not match" in r["error"]


def test_an_ipv6_nameserver_uses_an_ipv6_socket(udp):
    udp({ERP: ("answer", 0, ["10.9.1.5"])})
    dg.query_nameserver("2600:1700::1", ERP)
    assert FakeUdp.instances[0].family == socket.AF_INET6


def test_raw_probe_asks_at_most_two_nameservers(udp):
    udp({ERP: ("answer", 3, [])})
    r = dg.raw_probe(ERP, ["10.0.0.2", "10.0.0.3", "10.0.0.4"])
    assert [x["nameserver"] for x in r] == ["10.0.0.2", "10.0.0.3"]


def test_run_diagnostics_sends_dns_queries_only_for_the_erp_and_aws_names(net, udp, monkeypatch):
    net({ERP: [busy()], AWS: ["10.0.0.9"]}, {("10.0.0.9", 443): True})
    udp({ERP: ("answer", 3, []), AWS: ("answer", 0, ["10.0.0.9"])})
    monkeypatch.setattr(dg, "read_resolv", lambda path="/etc/resolv.conf": {"nameservers": ["10.0.0.2"], "search": []})
    d = dg.run_diagnostics({"SYTELINE_WSDL": f"https://{ERP}/x?wsdl"}, 5)
    sent = [q for inst in FakeUdp.instances for q in inst.sent]
    assert set(n for _, n in sent) == {ERP, AWS} and all(addr == ("10.0.0.2", 53) for addr, _ in sent)
    assert d["dns_raw"]["erp"][0]["rcode_name"] == "NXDOMAIN" and d["dns_raw"]["aws"][0]["answers"] == ["10.0.0.9"]


def test_no_nameservers_means_no_raw_query(net, udp, monkeypatch):
    net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"]}, {})
    udp({})
    monkeypatch.setattr(dg, "read_resolv", lambda path="/etc/resolv.conf": {"nameservers": [], "search": []})
    d = dg.run_diagnostics({"SYTELINE_WSDL": f"https://{ERP}/x?wsdl"}, 5)
    assert "dns_raw" not in d and FakeUdp.instances == []


def test_gethostbyname_is_a_second_opinion_only_when_getaddrinfo_fails(net, monkeypatch):
    net({ERP: [busy()]})
    monkeypatch.setattr(socket, "gethostbyname", lambda h: (_ for _ in ()).throw(OSError(errno.EBUSY, "Device or resource busy")))
    r = dg.probe_host(ERP, 443, 1)
    assert r["gethostbyname"]["ok"] is False and r["gethostbyname"]["errno"] == errno.EBUSY
    net({ERP: ["10.9.1.5"]}, {("10.9.1.5", 443): True})
    assert "gethostbyname" not in dg.probe_host(ERP, 443, 1)


def test_ipv6_connection_failures_are_marked_as_expected(net):
    net({}, {("2600:1f15::1", 443): OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")})
    bad = dg.connect("2600:1f15::1", 443, 1)
    assert bad["ok"] is False and "IPv6 is not available to Lambda" in bad["expected"]
    net({}, {("10.0.0.1", 443): OSError(errno.EADDRNOTAVAIL, "x")})
    assert "expected" not in dg.connect("10.0.0.1", 443, 1)  # only for IPv6


# ---- findings with the raw answer --------------------------------------------------------
def with_raw(d, results):
    d = dict(d)
    d["dns_raw"] = {"erp": results, "aws": None}
    return d


def raw(rcode_name="NOERROR", answers=(), ok=True):
    return {"nameserver": "169.254.100.5", "ok": ok, "rcode_name": rcode_name, "rcode": 0, "answers": list(answers)} \
        if rcode_name == "NOERROR" else {"nameserver": "169.254.100.5", "ok": ok, "rcode_name": rcode_name, "rcode": 3,
                                          "answers": list(answers)}


def test_the_nameserver_answers_but_the_function_lookup_fails_is_a_runtime_problem_not_dns():
    d = with_raw(diag(False, False, True, True), [raw("NOERROR", ["10.9.1.5"])])
    t = text(d)
    assert "answers this name correctly (10.9.1.5)" in t and "inside the function's runtime lookup" in t


def test_nxdomain_from_the_functions_nameserver():
    t = text(with_raw(diag(False, False, True, True), [raw("NXDOMAIN")]))
    assert "says the name does not exist (NXDOMAIN)" in t and "forwarding rule" in t


@pytest.mark.parametrize("name", ["SERVFAIL", "REFUSED"])
def test_servfail_and_refused_are_called_out(name):
    t = text(with_raw(diag(False, False, True, True), [raw(name)]))
    assert f"answered {name}" in t


def test_no_answer_from_any_nameserver_points_at_port_53():
    t = text(with_raw(diag(False, False, True, True), [{"nameserver": "10.0.0.2", "ok": False, "error": "no answer"}]))
    assert "No nameserver answered a direct DNS query" in t and "port 53" in t


def test_raw_findings_are_only_added_when_the_erp_lookup_failed():
    d = with_raw(diag(True, True, True, True), [raw("NXDOMAIN")])
    assert not any("NXDOMAIN" in f for f in dg.findings(d))


# ---- the database probe (spec FR-019; research.md R27 to R29) ---------------------------------------------------------
DB = "sql.plant.example"


def probe_cfg(**extra):
    return {"SYTELINE_WSDL": f"http://{ERP}/x?wsdl", "SQL_SERVER": DB, "SQL_PORT": "1433", **extra}


def ns_answer(name, rcode_name=None, answers=()):
    ok = rcode_name is not None
    return [{"nameserver": "10.0.0.2", "name": name, "ok": ok, **({"rcode": 0 if rcode_name == "NOERROR" else 3,
             "rcode_name": rcode_name, "answers": list(answers)} if ok else {"error": "no answer"})}]


@pytest.fixture
def fake_dns(monkeypatch):
    monkeypatch.setattr(dg, "read_resolv", lambda *a, **k: {"nameservers": ["10.0.0.2"], "search": []})
    answers = {}
    monkeypatch.setattr(dg, "raw_probe", lambda name, ns: answers.get(name, ns_answer(name, "NOERROR", ["10.1.2.3"])))
    return answers


def test_the_database_is_not_probed_unless_asked_and_the_output_is_unchanged(net, fake_dns):
    n = net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"]}, {("10.9.1.5", 80): True, ("10.0.0.9", 443): True})
    d = dg.run_diagnostics(probe_cfg(), 5)
    assert "database" not in d and "database_skipped" not in d and DB not in n.lookups
    assert set(d["dns_raw"]) == {"erp", "aws"} and not any("database" in f for f in d["findings"])


def test_the_probe_reports_lookup_nameserver_answer_and_a_connection_per_address(net, fake_dns):
    addrs = ["10.1.2.%d" % i for i in range(1, 7)]
    n = net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"], DB: [addrs]},
            {("10.9.1.5", 80): True, ("10.0.0.9", 443): True, **{(a, 1433): True for a in addrs}})
    d = dg.run_diagnostics(probe_cfg(), 5, database=True)
    db = d["database"]
    assert db["host"] == DB and db["port"] == 1433 and db["ok"] and db["connected"]
    assert len(db["connections"]) == dg.MAX_ADDRESSES and [c for c in n.connections if c[1] == 1433] and len(
        [c for c in n.connections if c[1] == 1433]) == dg.MAX_ADDRESSES
    assert d["dns_raw"]["database"][0]["name"] == DB
    assert any("The database host and port are reachable" in f for f in d["findings"])


def test_a_missing_server_or_a_bad_port_is_reported_as_skipped(net, fake_dns):
    net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"]})
    d = dg.run_diagnostics(probe_cfg(SQL_SERVER=""), 5, database=True)
    assert d["database_skipped"] == "the secret holds no SQL_SERVER" and "database" not in d
    d = dg.run_diagnostics(probe_cfg(SQL_PORT="14x"), 5, database=True)
    assert d["database_skipped"] == "SQL_PORT is not a number" and "database" not in d


def test_finding_for_a_name_the_nameserver_says_does_not_exist(net, fake_dns):
    net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"]}, {("10.9.1.5", 80): True, ("10.0.0.9", 443): True})
    fake_dns[DB] = ns_answer(DB, "NXDOMAIN")
    d = dg.run_diagnostics(probe_cfg(), 5, database=True)
    assert d["database"]["ok"] is False
    assert any("database name does not exist (NXDOMAIN)" in f for f in d["findings"])


def test_finding_for_a_resolved_host_whose_port_does_not_answer(net, fake_dns):
    net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"], DB: ["10.1.2.3"]}, {("10.9.1.5", 80): True, ("10.0.0.9", 443): True})
    d = dg.run_diagnostics(probe_cfg(), 5, database=True)
    assert d["database"]["ok"] and not d["database"]["connected"]
    found = [f for f in d["findings"] if "no connection to port 1433" in f]
    assert found and "security group" in found[0] and "SQL Server not listening" in found[0]


def test_finding_when_the_nameserver_knows_the_name_but_the_function_lookup_fails(net, fake_dns):
    net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"]}, {("10.9.1.5", 80): True, ("10.0.0.9", 443): True})
    d = dg.run_diagnostics(probe_cfg(), 5, database=True)  # fake nameserver answers 10.1.2.3, the function's lookup fails
    assert any("answers the database name correctly (10.1.2.3)" in f for f in d["findings"])


def test_the_probe_sends_nothing_but_lookups_and_connects(net, fake_dns):
    n = net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"], DB: ["10.1.2.3"]},
            {("10.9.1.5", 80): True, ("10.0.0.9", 443): True, ("10.1.2.3", 1433): True})
    d = dg.run_diagnostics(probe_cfg(SQL_USER="svc-login", SQL_PASS="S3cr3t-DB-Pass"), 5, database=True)
    assert sorted(set(n.connections)) == [("10.0.0.9", 443), ("10.1.2.3", 1433), ("10.9.1.5", 80)]  # connects only
    assert "pytds" not in vars(dg) and "SqlServer" not in vars(dg)  # no driver, so no login or query is possible
    assert "S3cr3t-DB-Pass" not in repr(d) and "svc-login" not in repr(d)
