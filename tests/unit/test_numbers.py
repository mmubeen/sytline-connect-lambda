import threading

import pytest

from fakes import DB_CFG, FakeSoap, FakeSqlServer
from syteline import errors, idos, numbers
from syteline.errors import ErpRejected, ErpUncertain, ErpUnreachable, IntegrationError, LiveWritesDisabled

DB = {idos.NEXT_CO_SETTING: "database", idos.NEXT_JOB_SETTING: "database"}


@pytest.fixture
def db_route(monkeypatch):
    for k, v in DB.items():
        monkeypatch.setenv(k, v)


def source(soap=None, cfg=None):
    return numbers.NumberSource(soap or FakeSoap(), dict(DB_CFG) if cfg is None else cfg)


def test_database_route_returns_co_then_job_numbers_over_one_connection(monkeypatch, db_route, live_writes):
    f = FakeSqlServer().install(monkeypatch)
    soap = FakeSoap()
    with source(soap) as n:
        assert [n.next_co(), n.next_job(), n.next_job(), n.next_job()] == ["100", "5000", "5001", "5002"]
        assert f.opened == 1 and f.closed == 0
    assert f.opened == f.closed == 1 and f.committed == [100, 5000, 5001, 5002]
    assert soap.calls == []  # the web service was never touched


def test_live_writes_off_refuses_before_any_connection(monkeypatch, db_route):
    f = FakeSqlServer().install(monkeypatch)
    with source() as n:
        with pytest.raises(LiveWritesDisabled):
            n.next_co()
    assert f.connect_kwargs == [] and f.statements == []


def test_missing_database_settings_refuse_before_any_connection(monkeypatch, db_route, live_writes):
    f = FakeSqlServer().install(monkeypatch)
    with source(cfg={}) as n:
        with pytest.raises(IntegrationError) as ei:
            n.next_co()
    assert ei.value.err["code"] == errors.DATABASE_NOT_CONFIGURED and f.connect_kwargs == []


def test_a_routine_error_is_rolled_back_and_rejected_and_the_next_call_is_not_blocked(monkeypatch, db_route, live_writes):
    f = FakeSqlServer(scenarios={("batch", 1): "error"}).install(monkeypatch)
    with source() as n:
        with pytest.raises(ErpRejected):
            n.next_co()
        assert n.next_co() == "100"  # rolled back: the failed attempt reserved nothing, the connection is still usable
    assert f.rolled_back == 1 and f.committed == [100]


def test_a_connection_lost_after_the_batch_is_uncertain(monkeypatch, db_route, live_writes):
    FakeSqlServer(scenarios={("batch", 1): "drop"}).install(monkeypatch)
    with source() as n:
        with pytest.raises(ErpUncertain):
            n.next_co()


@pytest.mark.parametrize("mode", ["connect_fail", "login_fail"])
def test_connect_and_login_failures_mean_nothing_happened(monkeypatch, db_route, live_writes, mode):
    FakeSqlServer(connect_error=mode).install(monkeypatch)
    with source() as n:
        with pytest.raises(ErpUnreachable):
            n.next_co()


def test_web_service_route_uses_the_service_and_never_the_database(monkeypatch, live_writes):
    monkeypatch.setenv(idos.NEXT_CO_SETTING, "MyCos")
    monkeypatch.setenv(idos.NEXT_JOB_SETTING, "MyJobs")
    f = FakeSqlServer().install(monkeypatch)
    soap = FakeSoap()
    with source(soap) as n:
        assert (n.next_co(), n.next_job()) == ("100", "5000")
    assert f.connect_kwargs == [] and soap.calls == [("invoke", "NextCoSp"), ("invoke", "NextWOSp")]


def test_an_absent_setting_keeps_the_earlier_web_service_route(monkeypatch, live_writes):
    f = FakeSqlServer().install(monkeypatch)
    with source(FakeSoap()) as n:
        assert n.next_co() == "100"
    assert f.connect_kwargs == []


def test_the_operations_are_routed_independently(monkeypatch, live_writes):
    monkeypatch.setenv(idos.NEXT_CO_SETTING, "database")
    monkeypatch.setenv(idos.NEXT_JOB_SETTING, "SLJobs")
    f = FakeSqlServer().install(monkeypatch)
    soap = FakeSoap()
    with source(soap) as n:
        assert (n.next_co(), n.next_job()) == ("100", "5000")
    assert f.committed == [100] and soap.calls == [("invoke", "NextWOSp")]


@pytest.mark.parametrize("web_failure", ["reject", "timeout_after", "timeout_before", "unreachable"])
def test_a_web_service_failure_never_starts_the_database_and_vice_versa(monkeypatch, live_writes, web_failure):
    """SC-009: no failure, timeout or unknown outcome on one route ever starts the same operation on the other."""
    monkeypatch.setenv(idos.NEXT_CO_SETTING, "SLCos")
    f = FakeSqlServer().install(monkeypatch)
    soap = FakeSoap(scenarios={("invoke", "NextCoSp", 1): web_failure})
    with source(soap) as n:
        with pytest.raises(Exception):
            n.next_co()
    assert f.connect_kwargs == [] and soap.calls == [("invoke", "NextCoSp")]
    monkeypatch.setenv(idos.NEXT_CO_SETTING, "database")
    f2 = FakeSqlServer(scenarios={("batch", 1): "drop"}).install(monkeypatch)
    soap2 = FakeSoap()
    with source(soap2) as n:
        with pytest.raises(ErpUncertain):
            n.next_co()
    assert soap2.calls == [] and len(f2.statements) >= 1


def test_twenty_concurrent_reservations_get_distinct_numbers(monkeypatch, db_route, live_writes):
    FakeSqlServer().install(monkeypatch)
    out, lock = [], threading.Lock()

    def one():
        with source() as n:
            res = [n.next_co()] + [n.next_job() for _ in range(5)]
        with lock:
            out.append(res)

    threads = [threading.Thread(target=one) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len({r[0] for r in out}) == 20 and len({j for r in out for j in r[1:]}) == 100


def test_close_is_idempotent_and_closes_the_connection(monkeypatch, db_route, live_writes):
    f = FakeSqlServer().install(monkeypatch)
    n = source()
    n.next_co()
    n.close()
    n.close()
    assert f.opened == f.closed == 1
