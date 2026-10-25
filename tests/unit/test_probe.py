import lambda_function
from syteline import errors, probe
from syteline.errors import ErpRejected, ErpUnreachable


class Soap:
    """Accepts the names in `known`; refuses any other like the service does."""

    def __init__(self, known, down_at=None):
        self.known, self.calls, self.down_at = set(known), [], down_at

    def load(self, ido, props, flt="", record_cap=0):
        self.calls.append((ido, tuple(props), flt, record_cap))
        if self.down_at is not None and len(self.calls) > self.down_at:
            raise ErpUnreachable("down")
        if props[0] not in self.known:
            raise ErpRejected(f"property [{props[0]}] does not exist in [{ido}] IDO.")
        return [{props[0]: "a value that must never be returned"}]


def test_each_name_is_asked_alone_in_a_one_row_read_and_only_verdicts_come_back():
    soap = Soap({"CoLine", "Item"})
    out = probe.probe(soap, {"ido": "CCT_Coitems", "properties": ["CoNum", "CoLine", "Item"]})
    assert out == {"ok": True, "action": "probe_properties", "ido": "CCT_Coitems", "exists": ["CoLine", "Item"],
                   "missing": [{"property": "CoNum", "error": "property [CoNum] does not exist in [CCT_Coitems] IDO."}]}
    assert soap.calls == [("CCT_Coitems", ("CoNum",), "", 1), ("CCT_Coitems", ("CoLine",), "", 1), ("CCT_Coitems", ("Item",), "", 1)]
    assert "must never be returned" not in str(out)


def test_a_known_ido_has_a_default_list_and_an_unknown_one_needs_names():
    out = probe.probe(Soap(set()), {"ido": "CCTJobs"})
    assert out["ok"] and len(out["missing"]) == len(probe.DEFAULTS["CCTJobs"])
    bad = probe.probe(Soap(set()), {"ido": "Mystery"})
    assert bad["ok"] is False and bad["errors"][0]["code"] == errors.VALIDATION


def test_bad_input_is_refused_before_any_call():
    soap = Soap(set())
    for event in ({}, {"ido": "a b"}, {"ido": "SLCos", "properties": []}, {"ido": "SLCos", "properties": ["x'y"]},
                  {"ido": "SLCos", "properties": ["a"] * 61}, {"ido": "SLCos", "properties": "CoNum"}):
        assert probe.probe(soap, event)["ok"] is False
    assert soap.calls == []


def test_an_unreachable_erp_stops_and_says_what_was_not_tried():
    out = probe.probe(Soap({"A", "B", "C"}, down_at=1), {"ido": "SLCos", "properties": ["A", "B", "C"]})
    assert out["ok"] is False and out["exists"] == ["A"] and out["not_tried"] == ["B", "C"]


def test_the_action_is_reachable_from_the_handler(monkeypatch):
    monkeypatch.setattr(lambda_function, "_client_factory", lambda: Soap({"CoLine"}))
    out = lambda_function.lambda_handler({"action": "probe_properties", "ido": "CCT_Coitems", "properties": ["CoLine", "CoNum"]}, None)
    assert out["ok"] and out["exists"] == ["CoLine"] and out["missing"][0]["property"] == "CoNum"


# ---- {"list": true}: the property names an IDO declares -----------------------------------------------------------------
class Lister:
    def __init__(self, names=("A", "B"), error=None):
        self.names, self.error, self.asked = list(names), error, []

    def columns(self, ido):
        self.asked.append(ido)
        if self.error:
            raise self.error
        return "*", self.names


def test_list_returns_the_declared_names_only():
    out = probe.probe(Lister(), {"ido": "CCT_Coitems", "list": True})
    assert out == {"ok": True, "action": "probe_properties", "ido": "CCT_Coitems", "property_list": "*", "columns": ["A", "B"]}


def test_list_reports_a_refusal_and_bad_input():
    out = probe.probe(Lister(error=ErpRejected("no such IDO")), {"ido": "Nope", "list": True})
    assert out["ok"] is False and out["errors"][0]["code"] == errors.ERP_REJECTED and out["ido"] == "Nope"
    assert probe.probe(Lister(), {"ido": "a b", "list": True})["errors"][0]["code"] == errors.VALIDATION
    assert probe.probe(Soap(set()), {"ido": "SLCos", "list": True})["errors"][0]["code"] == errors.NOT_IMPLEMENTED
