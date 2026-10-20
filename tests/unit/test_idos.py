import re

from syteline import idos


def test_quote_doubles_apostrophes():
    assert idos.quote("O'Brien") == "'O''Brien'"


def test_injection_attempt_stays_one_literal():
    f = idos.filter_eq("Name", "x' OR 1=1; DROP TABLE co;--")
    assert f == "Name = 'x'' OR 1=1; DROP TABLE co;--'"
    inner = f[len("Name = '"):-1]
    assert "'" not in inner.replace("''", "")  # every apostrophe inside is doubled


def test_prefix_neutralizes_like_wildcards():
    assert idos.filter_prefix("Name", "AC%_[") == "Name LIKE 'AC[%][_][[]%'"
    assert re.fullmatch(r"Name LIKE '[A-Z]+%'", idos.filter_prefix("Name", "ACME"))


def test_env_overrides(monkeypatch):
    assert idos.next_co() == ("SLCos", "NextCoSp")
    monkeypatch.setenv("SYTELINE_NEXT_CO_IDO", "OtherIdo")
    monkeypatch.setenv("SYTELINE_CUSTOMER_IDO", "MyCust")
    assert idos.next_co()[0] == "OtherIdo" and idos.customer_ido() == "MyCust"
