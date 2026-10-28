"""The line's due date: received date + the contract's (else the company default) due period (feature 012)."""

from fakes import FakeSoap
from syteline import duedate
from syteline.errors import ErpUnreachable


def soap(company=None, **contract):
    s = FakeSoap(item_master=False, company_due=company)
    if contract:
        s.add_record("SLItemcusts", CustNum="     C1", Item="R* A", **contract)
    return s


def test_a_contract_period_wins_over_the_default():
    assert duedate.period(soap(company=14, DuePeriod="30"), "C1", "R* A") == (30, "contract")


def test_an_unusable_contract_period_falls_to_the_default():
    for bad in ("", "0", "-5", "soon", "2.5"):
        assert duedate.period(soap(company=14, DuePeriod=bad), "C1", "R* A") == (14, "default"), bad


def test_another_customer_or_item_does_not_match_the_contract():
    s = soap(company=7, DuePeriod="30")
    assert duedate.period(s, "C2", "R* A") == (7, "default") and duedate.period(s, "C1", "R* B") == (7, "default")


def test_no_source_gives_none_and_a_bad_default_is_ignored():
    assert duedate.period(soap(), "C1", "R* A") == (None, "none")
    assert duedate.period(soap(company="x"), "C1", "R* A") == (None, "none")


def test_a_refused_or_unreachable_read_gives_none_without_raising():
    class Down(FakeSoap):
        def load(self, *a, **k):
            raise ErpUnreachable("down")

        read_back = load

    assert duedate.period(Down(), "C1", "R* A") == (None, "none")


def test_each_pair_and_the_default_are_read_once_per_cache():
    s, cache = soap(company=14), {}
    for _ in range(3):
        duedate.period(s, "C1", "R* A", cache)
    assert [c[1] for c in s.calls].count("SLItemcusts") == 1 and [c[1] for c in s.calls].count("SLCoparms") == 1


def test_due_date_adds_days_across_months_and_year_ends():
    assert duedate.due_date("2026-10-19", 14) == "2026-11-02T00:00:00-05:00"
    assert duedate.due_date("12/20/2026", 30) == "2027-01-19T00:00:00-05:00"
    assert duedate.due_date("2026-03-01", 1) == "2026-03-02T00:00:00-05:00"


def test_due_date_refuses_a_bad_date_or_days():
    assert duedate.due_date("not a date", 5) is None and duedate.due_date("", 5) is None and duedate.due_date(None, 5) is None
    assert duedate.due_date("2026-10-19", 0) is None and duedate.due_date("2026-10-19", None) is None and duedate.due_date("2026-10-19", -1) is None
