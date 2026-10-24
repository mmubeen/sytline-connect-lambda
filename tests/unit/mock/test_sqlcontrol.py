"""SQL Server control: provisioning, reset, scenario scripts and the sql.* faults (statements sent as `sa`)."""

import pytest

from edge import sqlcontrol


class FakeCursor:
    def __init__(self, log, fail=None):
        self.log, self.fail = log, fail

    def execute(self, sql, params=()):
        if self.fail and self.fail in sql:
            raise RuntimeError("boom")
        self.log.append(sql.strip())

    def close(self):
        pass

    def fetchone(self):
        return (1,)


class FakeConn:
    def __init__(self, log, fail=None):
        self.log, self.fail = log, fail
        self.closed = False

    def cursor(self):
        return FakeCursor(self.log, self.fail)

    def close(self):
        self.closed = True


class Proxy:
    mode = "unset"

    def set_mode(self, mode, delay_ms=0):
        self.mode = (mode, delay_ms)


@pytest.fixture
def ctl():
    log = []
    proxy = Proxy()
    c = sqlcontrol.SqlControl("sqlserver-real", 1433, "SaPass1!", "sytline_mock", "UserPass1!", "SyteLine_Mock",
                              proxy=proxy, connect=lambda **kw: FakeConn(log, kw.get("_fail")))
    c.log, c.proxy = log, proxy
    return c


def test_split_batches_on_go_lines():
    assert sqlcontrol.batches("A;\nGO\nB;\n  go  \nC;") == ["A;", "B;", "C;"]


def test_provision_fills_the_placeholders_and_runs_every_batch(ctl):
    ctl.provision()
    text = "\n".join(ctl.log)
    assert "CREATE DATABASE [SyteLine_Mock]" in text and "[sytline_mock]" in text and "UserPass1!" in text
    assert "@SQL_" not in text
    assert any("NextCoSp" in s for s in ctl.log) and any("NextWOSp" in s for s in ctl.log)


def test_reset_clears_tables_flags_and_enables_the_login(ctl):
    ctl.reset()
    text = "\n".join(ctl.log)
    for needle in ("DELETE FROM dbo.custaddr", "DELETE FROM dbo.co", "DELETE FROM dbo.mock_flags", "ENABLE"):
        assert needle in text
    assert ctl.proxy.mode == (None, 0)


def test_run_executes_a_scenario_script_in_the_mock_database(ctl):
    ctl.run("INSERT dbo.co (co_num) VALUES ('1');\nGO\nSELECT 1;")
    assert ctl.log[0].startswith("USE [SyteLine_Mock]") and "INSERT dbo.co" in "\n".join(ctl.log)


def test_login_refused_disables_the_login(ctl):
    ctl.fault("sql.login_refused", {})
    assert any("ALTER LOGIN [sytline_mock] DISABLE" in s for s in ctl.log)


def test_routine_error_and_slow_set_flags(ctl):
    ctl.fault("sql.routine_error", {})
    ctl.fault("sql.slow", {"delay_ms": 1500})
    text = "\n".join(ctl.log)
    assert "'routine_error'" in text and "'slow_ms'" in text and "1500" in text


def test_unreachable_refuses_at_the_proxy(ctl):
    ctl.fault("sql.unreachable", {})
    assert ctl.proxy.mode == ("refuse", 0)


def test_clear_faults_undoes_all_of_them(ctl):
    ctl.clear_faults()
    text = "\n".join(ctl.log)
    assert "DELETE FROM dbo.mock_flags" in text and "ENABLE" in text and ctl.proxy.mode == (None, 0)


def test_values_cannot_inject_sql(ctl):
    with pytest.raises(ValueError):
        sqlcontrol.SqlControl("h", 1, "p", "bad]user", "x", "db")
    with pytest.raises(ValueError):
        ctl.fault("sql.slow", {"delay_ms": "1; DROP TABLE x"})


def test_healthy_is_false_when_the_server_cannot_be_reached():
    def boom(**kw):
        raise OSError("down")

    c = sqlcontrol.SqlControl("h", 1, "p", "u", "x", "db", connect=boom)
    assert c.healthy() is False
