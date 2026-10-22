"""The database is used ONLY for the three calls of feature 005 (reserve numbers, find the customer, read the customer's
default codes), only through src/syteline/sqlserver.py, and it can never change a row directly (FR-009, SC-005).

(Features 001 to 004 had a stricter rule: no database connection at all. It is relaxed for exactly these calls.)
"""

import os
import re

import pytest

from fakes import DB_CFG, FakeSqlServer
from syteline import sqlserver

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SCR = os.path.join(ROOT, "src")
SQL_MODULES = ("pytds", "pyodbc", "pymssql", "psycopg2", "sqlalchemy", "cx_Oracle")
ALLOWED_IMPORTER = os.path.join("src", "syteline", "sqlserver.py")


def py_files():
    for base, _dirs, files in os.walk(SCR):
        if "__pycache__" in base:
            continue
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(base, f)


def test_only_sqlserver_py_imports_a_sql_client_and_only_the_one_driver():
    pat = re.compile(r"^\s*(?:import|from)\s+(%s)\b" % "|".join(SQL_MODULES), re.M)
    found = {}
    for p in py_files():
        mods = {m.group(1) for m in pat.finditer(open(p).read())}
        if mods:
            found[os.path.relpath(p, ROOT)] = mods
    assert found == {ALLOWED_IMPORTER: {"pytds"}}


def test_no_other_sql_module_file():
    assert not os.path.exists(os.path.join(SCR, "syteline", "sql.py"))


def test_requirements_list_the_one_driver_and_no_odbc_or_other_sql_client():
    text = open(os.path.join(SCR, "requirements.txt")).read().lower()
    assert "python-tds" in text
    assert not any(name in text for name in ("pyodbc", "odbc", "mssql", "sqlalchemy", "psycopg"))


def test_the_database_settings_are_exactly_the_declared_ones():
    from syteline import config

    keys = {k for k in config.load_config() if "SQL" in k}
    assert keys == set(config.SQL_REQUIRED) | {"SQL_PORT"}


# ---- statement audit (SC-005) ---------------------------------------------------------------------------
WHITELIST = sqlserver._ALLOWED
CHANGING = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT)\b", re.I)


def exercise(db):
    db.next_number("co")
    db.next_number("job")
    db.candidates(["acme", "o'brien"], "85001", 1000)
    db.latest_order_codes("C1", 0)
    db.latest_order_codes("C1", None)
    db.ping()


def assert_only_whitelisted(statements):
    assert statements, "the audit saw no statements"
    for sql, params in statements:
        assert any(p.match(sql) for p in WHITELIST), f"not on the whitelist: {sql[:80]!r}"
        if sql.startswith("SET NOCOUNT ON"):
            # the numbering batch: its BEGIN TRANSACTION is the routine's own, the only other verbs are DECLARE/EXEC/SELECT
            assert not CHANGING.search(sql.replace("BEGIN TRANSACTION", "")), sql
        else:
            assert not CHANGING.search(sql), sql
        assert all(isinstance(p, (str, int)) for p in params)  # every value travels as a parameter


def test_every_statement_the_database_module_runs_is_on_the_whitelist_and_changes_no_row(monkeypatch):
    f = FakeSqlServer(custaddr=[{"cust_num": "C1", "cust_seq": 0, "name": "ACME", "zip": "85001"}],
                      co_rows=[{"co_num": 1, "cust_num": "C1", "cust_seq": 0, "terms_code": "N30",
                                "ship_code": "UPS"}]).install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        exercise(db)
    assert_only_whitelisted(f.statements)
    kinds = {("NextCoSp" if "NextCoSp" in s else "NextWOSp" if "NextWOSp" in s else s.split()[0]) for s, _ in f.statements}
    assert {"NextCoSp", "NextWOSp", "COMMIT", "SELECT"} <= kinds


def test_failure_paths_also_send_only_whitelisted_statements(monkeypatch):
    f = FakeSqlServer(scenarios={("batch", 1): "error", ("batch", 2): "null", ("commit", 1): "drop"}).install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        for kind in ("co", "co", "job"):
            with pytest.raises(Exception):
                db.next_number(kind)
    assert_only_whitelisted(f.statements)
    assert f.rolled_back >= 2


def test_a_statement_outside_the_whitelist_is_refused_before_it_is_sent(monkeypatch):
    f = FakeSqlServer().install(monkeypatch)
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        with pytest.raises(RuntimeError):
            db._run("UPDATE co SET terms_code = 'X'")
    assert f.statements == []
