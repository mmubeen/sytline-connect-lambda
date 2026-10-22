import logging
import os
import stat
import threading

import pytest

from fakes import DB_CFG, FakeSqlServer
from syteline import errors, sqlserver
from syteline.errors import ErpRejected, ErpUncertain, ErpUnreachable, IntegrationError


@pytest.fixture
def fake(monkeypatch):
    return FakeSqlServer().install(monkeypatch)


# ---- connection: always encrypted, verified, never without a CA ----------------------------------------
def test_missing_settings_are_refused_before_any_connection_naming_only_the_keys(fake):
    cfg = dict(DB_CFG, SQL_PASS="", SQL_CA_PEM="")
    with pytest.raises(IntegrationError) as ei:
        with sqlserver.SqlServer(cfg):
            pass
    assert ei.value.err["code"] == errors.DATABASE_NOT_CONFIGURED
    assert "SQL_PASS" in ei.value.err["message"] and "SQL_CA_PEM" in ei.value.err["message"]
    assert DB_CFG["SQL_SERVER"] not in ei.value.err["message"] and fake.connect_kwargs == []


def test_there_is_no_connection_without_a_ca_certificate(fake):
    with pytest.raises(IntegrationError):
        with sqlserver.SqlServer(dict(DB_CFG, SQL_CA_PEM="  ")):
            pass
    assert fake.connect_kwargs == [] and fake.opened == 0


def test_the_connection_is_encrypted_verified_and_cleaned_up(fake, monkeypatch):
    seen = {}

    def spy(**kw):
        seen["mode"] = stat.S_IMODE(os.stat(kw["cafile"]).st_mode)
        seen["pem"] = open(kw["cafile"]).read()
        return fake.connect(**kw)

    monkeypatch.setattr(sqlserver, "_connect", spy)
    with sqlserver.SqlServer(dict(DB_CFG)):
        kw = fake.connect_kwargs[0]
        path = kw["cafile"]
        assert kw["server"] == "sql.plant.example" and kw["database"] == "SL_PROD" and kw["port"] == 1433
        assert kw["validate_host"] is True and kw["autocommit"] is True
        assert (kw["login_timeout"], kw["timeout"]) == (5.0, 10.0)
        assert seen["mode"] == 0o600 and seen["pem"] == DB_CFG["SQL_CA_PEM"]
    assert not os.path.exists(path) and fake.opened == fake.closed == 1


def test_the_host_name_check_can_be_switched_off_only_explicitly_and_warns_every_time(fake, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setenv("SYTELINE_SQL_VERIFY_HOSTNAME", "false")
    for _ in range(2):
        with sqlserver.SqlServer(dict(DB_CFG)):
            pass
    assert [k["validate_host"] for k in fake.connect_kwargs] == [False, False]
    assert len([r for r in caplog.records if "host-name check OFF" in r.getMessage()]) == 2
    monkeypatch.setenv("SYTELINE_SQL_VERIFY_HOSTNAME", "0")  # anything but exactly "false" keeps it on
    with sqlserver.SqlServer(dict(DB_CFG)):
        pass
    assert fake.connect_kwargs[-1]["validate_host"] is True


def test_connect_and_login_failures_are_unreachable_and_leak_no_credentials(monkeypatch):
    for mode in ("connect_fail", "login_fail"):
        f = FakeSqlServer(connect_error=mode).install(monkeypatch)
        with pytest.raises(ErpUnreachable) as ei:
            with sqlserver.SqlServer(dict(DB_CFG)):
                pass
        assert "S3cr3t-DB-Pass" not in str(ei.value) and "svc-login" not in str(ei.value)
        assert f.opened == 0


def test_port_comes_from_the_settings(fake):
    with sqlserver.SqlServer(dict(DB_CFG, SQL_PORT="14330")):
        pass
    assert fake.connect_kwargs[0]["port"] == 14330


# ---- numbering ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["{n}", "^{n}^", "^{n},{m}^"])
def test_next_number_commits_and_reads_the_first_integer_of_every_key_format(monkeypatch, fmt):
    f = FakeSqlServer(key_format=fmt).install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        assert db.next_number("co") == "100" and db.next_number("job") == "5000" and db.next_number("job") == "5001"
    assert f.committed == [100, 5000, 5001] and f.rolled_back == 0
    texts = f.statements_text()
    assert "EXEC [dbo].[NextCoSp]" in texts[0] and "EXEC [dbo].[NextWOSp]" in texts[2]
    assert texts[1] == "COMMIT TRANSACTION" and f.statements[0][1] == (10,)  # site id as a parameter


@pytest.mark.parametrize("action,reason", [("error", "failed"), ("null", "no number"), ("bad", "unreadable"),
                                           ("empty", "no number")])
def test_a_routine_error_or_unusable_answer_is_rolled_back_and_rejected(monkeypatch, action, reason):
    f = FakeSqlServer(scenarios={("batch", 1): action}).install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        with pytest.raises(ErpRejected) as ei:
            db.next_number("co")
    assert "NextCoSp" in str(ei.value) and reason in str(ei.value)
    assert f.rolled_back == 1 and f.committed == []


def test_a_connection_lost_after_the_batch_or_during_the_commit_is_uncertain(monkeypatch):
    f = FakeSqlServer(scenarios={("batch", 1): "drop", ("commit", 1): "drop"}).install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        with pytest.raises(ErpUncertain):
            db.next_number("co")  # lost after the batch was sent
    f2 = FakeSqlServer(scenarios={("commit", 1): "drop"}).install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        with pytest.raises(ErpUncertain) as ei:
            db.next_number("job")
    assert "commit outcome unknown" in str(ei.value) and f2.committed == []


def test_infobar_text_is_a_warning_not_a_failure(monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    FakeSqlServer(infobar="Site 10 note").install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        assert db.next_number("co") == "100"
    assert any("Infobar: Site 10 note" in r.getMessage() for r in caplog.records)


def test_the_connection_is_closed_on_every_exit_path(monkeypatch):
    f = FakeSqlServer(scenarios={("batch", 1): "error"}).install(monkeypatch)
    with pytest.raises(ErpRejected):
        with sqlserver.SqlServer(dict(DB_CFG)) as db:
            db.next_number("co")
    assert f.opened == f.closed == 1
    with pytest.raises(RuntimeError):
        with sqlserver.SqlServer(dict(DB_CFG)):
            raise RuntimeError("caller bug")
    assert f.opened == f.closed == 2


def test_concurrent_connections_get_distinct_numbers(monkeypatch):
    f = FakeSqlServer().install(monkeypatch)
    out, lock = [], threading.Lock()

    def one():
        with sqlserver.SqlServer(dict(DB_CFG)) as db:
            n = [db.next_number("co")] + [db.next_number("job") for _ in range(3)]
        with lock:
            out.append(n)

    threads = [threading.Thread(target=one) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len({o[0] for o in out}) == 20 and len({j for o in out for j in o[1:]}) == 60
    assert f.opened == f.closed == 20


# ---- reads ----------------------------------------------------------------------------------------------
CUSTS = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME AEROSPACE INC", "addr1": "1 MAIN ST", "city": "PHOENIX",
          "state": "AZ", "zip": "85001", "country": "USA"},
         {"cust_num": "C2", "cust_seq": 0, "name": "BETA TOOLS", "zip": "60601"}]


def test_candidates_are_a_parameterised_select_and_capped(monkeypatch):
    f = FakeSqlServer(custaddr=CUSTS).install(monkeypatch)
    evil = "o'brien'; drop table co;--"
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        rows = db.candidates(["acme", evil], "60601", 1)
    assert [r["cust_num"] for r in rows] == ["C1"]  # cap 1, ordered by cust_num
    sql, params = f.statements[0]
    assert sql.startswith("SELECT TOP 1 ") and "LOWER(name) LIKE %s OR LOWER(name) LIKE %s OR zip LIKE %s" in sql
    assert params == ("%acme%", f"%{evil}%", "60601%") and evil not in sql
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        assert db.candidates([], None, 10) == []  # nothing to search for: no statement is sent
    assert len(f.statements) == 1


def test_latest_order_codes_same_ship_to_then_any_order_then_none(monkeypatch):
    co = [{"co_num": 1, "cust_num": "C1", "cust_seq": 0, "terms_code": "N30", "ship_code": "UPS"},
          {"co_num": 2, "cust_num": "C1", "cust_seq": 0, "terms_code": "N45", "ship_code": "FDX"},
          {"co_num": 9, "cust_num": "C1", "cust_seq": 3, "terms_code": "N10", "ship_code": "DHL"}]
    f = FakeSqlServer(co_rows=co).install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        assert db.latest_order_codes("C1", 0) == ("N45", "FDX", "same_ship_to")  # the newest for that ship-to
        assert db.latest_order_codes(" C1 ", 7) == ("N10", "DHL", "any_order")   # none for ship-to 7: newest overall
        assert db.latest_order_codes("C1", None) == ("N10", "DHL", "any_order")
        assert db.latest_order_codes("C9", 0) is None and db.latest_order_codes("", 0) is None


def test_a_failing_same_ship_to_query_falls_through_to_the_second_as_the_demo_does(monkeypatch):
    co = [{"co_num": 1, "cust_num": "C1", "cust_seq": 0, "terms_code": "N30", "ship_code": "UPS"}]
    FakeSqlServer(co_rows=co, scenarios={("select", 1): "error"}).install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        assert db.latest_order_codes("C1", 0) == ("N30", "UPS", "any_order")


def test_read_failures_are_classified_and_scrubbed(monkeypatch):
    FakeSqlServer(scenarios={("select", 1): "error", ("select", 2): "drop"}).install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        with pytest.raises(ErpRejected):
            db.ping()
        with pytest.raises(ErpUnreachable) as ei:  # a read has no side effect: a lost connection is just unreachable
            db.ping()
    assert "S3cr3t-DB-Pass" not in str(ei.value)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        assert db.ping() is True


def test_only_whitelisted_statements_can_run(monkeypatch):
    FakeSqlServer().install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        for bad in ("DELETE FROM co", "UPDATE co SET terms_code = 'x'", "SELECT * FROM co", "EXEC [dbo].[Evil]",
                    "INSERT INTO co VALUES (1)"):
            with pytest.raises(RuntimeError):
                db._run(bad)


def test_nothing_secret_reaches_any_log_record(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    FakeSqlServer(infobar="login svc-login pw S3cr3t-DB-Pass").install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        db.next_number("co")
    assert "S3cr3t-DB-Pass" not in caplog.text and "svc-login" not in caplog.text
