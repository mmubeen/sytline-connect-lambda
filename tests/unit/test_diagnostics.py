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


def test_local_info_distinguishes_vpc_attachment(net):
    net({}, local="169.254.76.1")
    assert dg.local_info()["likely_vpc_attached"] is False
    net({}, local="10.50.0.17")
    assert dg.local_info()["likely_vpc_attached"] is True


# ---- findings: what the combination means ------------------------------------------------
def diag(erp_dns, erp_tcp, aws_dns, aws_tcp, vpc=True, erp_addr="10.9.1.5", attempts=None):
    erp = {"host": ERP, "port": 80, "ok": erp_dns, "addresses": [erp_addr] if erp_dns else [],
           "attempts": attempts or [{"ok": erp_dns}], "connected": erp_tcp}
    aws = {"host": AWS, "port": 443, "ok": aws_dns, "addresses": ["10.0.0.9"] if aws_dns else [],
           "attempts": [{"ok": aws_dns}], "connected": aws_tcp}
    return {"erp": erp, "aws": aws, "local": {"likely_vpc_attached": vpc}}


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
    assert "transient-looking resolver error" in t and "General name resolution works" in t


def test_resolving_after_retries_is_called_out_as_transient():
    d = diag(True, True, True, True, attempts=[{"ok": False}, {"ok": False}, {"ok": True}])
    assert "only after 2 failed attempt" in text(d)


def test_erp_resolves_but_does_not_connect_while_aws_does():
    t = text(diag(True, False, True, True))
    assert "missing route to the ERP" in t and "security group or firewall" in t


def test_no_outbound_path_when_neither_connects():
    assert "no working outbound path" in text(diag(True, False, True, False))


def test_private_erp_address_from_a_function_outside_a_vpc():
    t = text(diag(True, False, True, True, vpc=False, erp_addr="10.9.1.5"))
    assert "not attached to a VPC" in t
    assert "not attached to a VPC" not in text(diag(True, False, True, True, vpc=False, erp_addr="52.0.0.1"))


def test_erp_ok_but_the_secret_store_unreachable():
    assert "cannot reach Secrets Manager" in text(diag(True, True, True, False))


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
    assert d["aws"]["host"] == AWS and any("transient-looking" in f for f in d["findings"])
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


def test_the_https_rule_still_wins_and_nothing_is_probed(net, cfg_env, monkeypatch):
    n = net({ERP: ["10.9.1.5"], AWS: ["10.0.0.9"]})
    monkeypatch.setenv("SYTELINE_REQUIRE_HTTPS", "true")
    out = health.run_health({"diagnose": True})
    assert out["errors"][0]["code"] == "INSECURE_TRANSPORT" and out["stages"] == []
    assert "HTTPS rule refused" in out["diagnostics"]["skipped"]
    assert n.lookups == [] and n.connections == []


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
