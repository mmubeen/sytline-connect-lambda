import json
import os

import pytest

from fakes import DB_CFG, FakeSqlServer
from syteline import customer_match as cm, sqlserver

GOLDEN = json.load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "customer_match_golden.json")))
CASES = GOLDEN["cases"]


def ids(c):
    return f"{c['name'][:18]}|{c['address'][:14]}".replace("\n", " ")


@pytest.fixture
def server(monkeypatch):
    return FakeSqlServer(custaddr=GOLDEN["table"]).install(monkeypatch)


def find(customer):
    with sqlserver.SqlServer(dict(DB_CFG)) as db:
        return cm.find(db, customer)


def test_the_golden_set_is_large_and_covers_every_status():
    assert len(CASES) >= 30
    assert {c["status"] for c in CASES} == {"match", "ambiguous", "low_confidence", "not_found"}
    assert (GOLDEN["min_score"], GOLDEN["margin"], GOLDEN["cap"]) == (70.0, 3.0, 1000)  # the demo's own settings


@pytest.mark.parametrize("case", CASES, ids=ids)
def test_every_golden_case_gets_the_demos_decision_and_scores(server, case):
    """SC-003: the same tokens, ZIP, status, best candidate and scores as the demo's own code."""
    if case["name"].strip() and case["address"].strip():  # the demo refuses an empty name or address before tokenising
        assert cm.name_tokens(case["name"]) == case["tokens"]
        assert cm.zip_token(case["address"]) == case["zip5"]
    got = find({"name": case["name"], "address1": case["address"]})
    assert got["status"] == case["status"]
    if case["best"] is None:
        assert "cust_num" not in got
        return
    assert (got["best"]["cust_num"], got["best"]["cust_seq"]) == (case["best"]["cust_num"], case["best"]["cust_seq"])
    assert got["best"]["score"] == pytest.approx(case["best"]["score"], abs=0.01)
    if case["runner_up"] is None:
        assert got["runner_up"] is None
    else:
        assert got["runner_up"]["score"] == pytest.approx(case["runner_up"]["score"], abs=0.01)
        assert (got["runner_up"]["cust_num"], got["runner_up"]["cust_seq"]) == (
            case["runner_up"]["cust_num"], case["runner_up"]["cust_seq"])


def test_a_match_carries_the_customer_and_ship_to_and_the_matched_name(server):
    got = find({"name": "Acme Aerospace, Inc.", "address1": "1 Main St", "city": "Phoenix", "state": "AZ",
                "postal_code": "85001"})
    assert got["status"] == "match" and (got["cust_num"], got["cust_seq"]) == ("C0001", 0)
    assert got["matched_name"] == "ACME AEROSPACE INC" and got["candidates_considered"] >= 1


def test_ambiguous_returns_both_candidates_with_scores(server):
    got = find({"name": "Delta Components", "address1": "300 South Ave Spokane WA 99201"})
    assert got["status"] == "ambiguous"
    assert {got["best"]["cust_num"], got["runner_up"]["cust_num"]} == {"C0006", "C0007"}
    assert got["best"]["score"] == got["runner_up"]["score"] == 100.0


def test_low_confidence_and_not_found(server):
    low = find({"name": "Sigma Hydraulics", "address1": "999 Nowhere Street Reno NV 89501"})
    assert low["status"] == "low_confidence" and low["best"]["cust_num"] == "C0010" and low["best"]["score"] < 70
    nf = find({"name": "Zeta Unknown Industries", "address1": "12 Elm Street Springfield IL 62701"})
    assert nf["status"] == "not_found" and "reason" in nf


def test_an_empty_name_or_address_sends_no_query(server):
    for customer in ({"name": "", "address1": "1 Main St"}, {"name": "Acme", "address1": ""}, {"name": " "}, {}):
        assert find(customer)["status"] == "not_found"
    assert server.statements == []


def test_the_search_text_is_only_ever_a_parameter(server):
    evil = "o'brien'; DROP TABLE co;--"
    find({"name": evil, "address1": "8 Church Court Boston MA 02108"})
    sql, params = server.statements[0]
    assert "drop" not in sql.lower() and "o'brien" not in sql.lower()  # nothing from the document is in the statement
    assert any("drop" in str(p).lower() for p in params)               # it travelled as a parameter value


def test_the_candidate_cap_and_thresholds_follow_the_settings(server, monkeypatch):
    monkeypatch.setenv("SYTELINE_MATCH_CAP", "1")
    got = find({"name": "Delta Components", "address1": "300 South Ave Spokane WA 99201"})
    assert server.statements[-1][0].startswith("SELECT TOP 1 ") and got["candidates_considered"] == 1
    assert got["status"] == "match"  # one candidate only: no runner-up to be ambiguous with
    monkeypatch.setenv("SYTELINE_MATCH_CAP", "1000")
    monkeypatch.setenv("SYTELINE_MATCH_MIN_SCORE", "99")
    assert find({"name": "Acme Aerospace Inc", "address1": "1 Main St Phoenix AZ 85001"})["status"] == "match"
    monkeypatch.setenv("SYTELINE_MATCH_MIN_SCORE", "100")
    assert find({"name": "Acme Aerospace Inc", "address1": "1 Main St Phoenix AZ 85034"})["status"] == "low_confidence"
    monkeypatch.setenv("SYTELINE_MATCH_MIN_SCORE", "70")
    monkeypatch.setenv("SYTELINE_MATCH_MARGIN", "50")
    assert find({"name": "Acme Aerospace Inc", "address1": "1 Main St Phoenix AZ 85001"})["status"] == "ambiguous"


def test_normalisation_matches_the_demo():
    assert cm.normalize("1 Main Street, Suite #5") == "1 main st ste 5"
    assert cm.normalize("Acme Corp. Inc") == "acme corporation incorporated"
    assert cm.clean_address("Attn: Bob\n1 Main St\nPhone: 555\nPhoenix AZ") == "1 Main St Phoenix AZ"
    assert cm.name_tokens("The Acme & Co, Inc. of Phoenix Aerospace") == ["acme", "phoenix"]
    assert cm.zip_token("Phoenix AZ 85001-1234") == "85001" and cm.zip_token("no zip") is None
