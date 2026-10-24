"""T057: the developer's credentials directory is mounted into the edge and nowhere else (FR-018)."""

import os

import yaml

COMPOSE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "mock", "compose.yaml")


def services():
    return yaml.safe_load(open(COMPOSE))["services"]


def mounts_idp(service):
    return any(".idp" in str(v) for v in service.get("volumes", []))


def test_only_the_edge_mounts_the_credentials():
    s = services()
    assert [name for name, svc in s.items() if mounts_idp(svc)] == ["edge"]


def test_the_credentials_are_mounted_read_only():
    vol = next(v for v in services()["edge"]["volumes"] if ".idp" in v)
    assert vol.endswith(":ro")


def test_no_service_other_than_the_edge_is_on_a_network_with_a_route_out():
    s = services()
    for name, svc in s.items():
        nets = svc.get("networks", [])
        names = list(nets) if not isinstance(nets, dict) else list(nets)
        assert ("outside" in names) == (name == "edge"), name


def test_the_function_services_get_no_credential_variables():
    s = services()
    for name in ("fn1", "fn2", "fn3"):
        env = s[name].get("environment", {}) or {}
        assert not any("IDP" in str(k) and "CRED" in str(k).upper() for k in env)
        assert [str(v) for v in s[name].get("volumes", []) if ".idp" in str(v) or "creds" in str(v)] == []
