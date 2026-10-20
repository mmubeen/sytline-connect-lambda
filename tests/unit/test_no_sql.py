"""FR-018: all SyteLine data access goes through the web service. No SQL anywhere."""

import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SCR = os.path.join(ROOT, "scr")
SQL_MODULES = ("pytds", "pyodbc", "pymssql", "psycopg2", "sqlalchemy", "cx_Oracle")


def py_files():
    for base, _dirs, files in os.walk(SCR):
        if "__pycache__" in base:
            continue
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(base, f)


def test_no_module_under_scr_imports_a_sql_client():
    pat = re.compile(r"^\s*(?:import|from)\s+(%s)\b" % "|".join(SQL_MODULES), re.M)
    assert [p for p in py_files() if pat.search(open(p).read())] == []


def test_no_sql_module_file():
    assert not os.path.exists(os.path.join(SCR, "syteline", "sql.py"))


def test_requirements_list_no_sql_driver():
    text = open(os.path.join(SCR, "requirements.txt")).read().lower()
    assert not any(name in text for name in ("tds", "odbc", "mssql", "sql"))


def test_no_sql_settings_anywhere():
    for rel in ("template.yaml", ".env.example"):
        assert "SQL" not in open(os.path.join(SCR, rel)).read(), rel
    for p in py_files():
        assert "SYTELINE_SQL" not in open(p).read(), p


def test_load_config_has_no_sql_keys():
    from syteline import config

    assert not [k for k in config.load_config() if "SQL" in k]
