import itertools

import pytest

from syteline import idos, routes

SETTINGS = {"next_co": idos.NEXT_CO_SETTING, "next_job": idos.NEXT_JOB_SETTING, "customer": idos.CUSTOMER_SETTING,
            "defaults": idos.DEFAULTS_SETTING}
LEGACY = {"next_co": "SLCos", "next_job": "SLJobs", "customer": "SLCustAddrs", "defaults": None}


@pytest.mark.parametrize("op", routes.OPERATIONS)
def test_absent_setting_keeps_the_earlier_web_service_route_and_defaults_are_off(op):
    kind, name = routes.route(op, {})
    assert (kind, name) == (("webservice", LEGACY[op]) if LEGACY[op] else ("off", None))


@pytest.mark.parametrize("op", routes.OPERATIONS)
@pytest.mark.parametrize("value", ["database", "DATABASE", " Database ", "", "   "])
def test_database_or_empty_means_the_database(op, value):
    assert routes.route(op, {SETTINGS[op]: value}) == ("database", None)


@pytest.mark.parametrize("op", routes.OPERATIONS)
def test_an_endpoint_name_means_the_web_service(op):
    assert routes.route(op, {SETTINGS[op]: " MyIdo "}) == ("webservice", "MyIdo")


def test_each_operation_is_decided_independently():
    env = {idos.NEXT_CO_SETTING: "database", idos.NEXT_JOB_SETTING: "SLJobs", idos.CUSTOMER_SETTING: "database"}
    assert routes.describe(env) == {"next_co": "database", "next_job": "webservice:SLJobs", "customer": "database",
                                    "defaults": "off"}
    assert routes.uses_database(env)
    assert not routes.uses_database({s: "X" for s in SETTINGS.values()})


def test_the_stand_alone_case_nothing_set(monkeypatch):
    for s in SETTINGS.values():
        monkeypatch.delenv(s, raising=False)
    assert routes.describe() == {"next_co": "webservice:SLCos", "next_job": "webservice:SLJobs",
                                 "customer": "webservice:SLCustAddrs", "defaults": "off"}
    assert not routes.uses_database()  # so the connectivity check shows no database stage and nothing connects


def test_the_earlier_override_variables_still_rename_the_legacy_default(monkeypatch):
    monkeypatch.delenv(idos.NEXT_CO_SETTING, raising=False)
    assert routes.route("next_co", {}) == ("webservice", "SLCos")


def test_unknown_operation_is_refused():
    with pytest.raises(ValueError):
        routes.route("nope", {})


def test_route_reads_only_the_environment_never_the_network(monkeypatch):
    import socket

    def boom(*a, **k):
        raise AssertionError("route() must not touch the network")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    for combo in itertools.product(["", "database", "X", None], repeat=4):
        env = {SETTINGS[op]: v for op, v in zip(routes.OPERATIONS, combo) if v is not None}
        routes.describe(env)
