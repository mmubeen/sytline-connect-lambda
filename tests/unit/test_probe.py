import lambda_function
from syteline import errors, idos, probe
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


# ---- probe_item: how the ERP holds one item -------------------------------------------------------------------------------
class ItemSoap:
    def __init__(self):
        self.calls = []

    def load(self, ido, props, flt="", record_cap=0):
        self.calls.append((ido, tuple(props), flt, record_cap))
        if "Description" in props:
            raise ErpRejected("property [Description] does not exist")
        return [{"Item": "R* 3810727-6", "ItmDescription": "MAT_OUT 3810727-6"}] if "LIKE" in flt else []


def test_probe_item_reads_the_item_master_three_ways_and_the_order_lines_without_writing():
    soap = ItemSoap()
    out = probe.probe_item(soap, {"item": "3810727-6", "co_num": "129556"})
    assert out["ok"] and out["code"] == "R* 3810727-6"
    im = out["item_master"]
    assert im["exact"]["rows"] == [] and im["padded"]["filter"] == "Item = '" + "R* 3810727-6".rjust(30) + "'"
    assert im["like"]["rows"][0]["ItmDescription"] == "MAT_OUT 3810727-6"
    assert "error" in out["order_lines"] and set(out["written_lines"]) == {"padded", "like"} and all(c[3] == 3 for c in soap.calls)


def test_probe_item_refuses_bad_input_and_is_reachable_from_the_handler(monkeypatch):
    assert probe.probe_item(ItemSoap(), {"item": "a%b"})["ok"] is False
    assert probe.probe_item(ItemSoap(), {"item": "A", "co_num": "1; drop"})["ok"] is False
    monkeypatch.setattr(lambda_function, "_client_factory", lambda: ItemSoap())
    assert lambda_function.lambda_handler({"action": "probe_item", "item": "3810727-6"}, None)["ok"]


def test_the_note_idos_have_default_candidate_names_for_the_one_by_one_probe():
    for ido in ("SpecificNotes", "SLObjectNotes", "ObjectNotes"):
        assert "SpecificNoteToken" in probe.DEFAULTS[ido]
    for ido in ("SLObjectNotes", "ObjectNotes"):
        assert {"NoteHeaderToken", "RefRowPointer"} <= set(probe.DEFAULTS[ido])
    assert {"NoteDesc", "NoteContent"} <= set(probe.DEFAULTS["SpecificNotes"]) and "NoteType" in probe.DEFAULTS["SLObjectNotes"]


# ---- probe_notes: the notes attached to an order ---------------------------------------------------------------------------
def test_probe_notes_lists_the_attached_notes_by_title_and_length_never_the_text():
    from fakes import FakeSoap

    s = FakeSoap()
    s.add_record("SLCos", CoNum="    129566", RowPointer="RP-1")
    s.add_record("SpecificNotes", SpecificNoteToken="7", NoteDesc="Customer Requirements", NoteContent="secret text", CreateDate="9999-01-01")
    s.add_record("SpecificNotes", SpecificNoteToken="8", NoteDesc="Billing Information", NoteContent="orphan", CreateDate="9999-01-01")
    s.add_record(idos.object_note_link_ido(), SpecificNoteToken="7", NoteHeaderToken=31, RefRowPointer="RP-1", NoteType=0)
    out = probe.probe_notes(s, {"co_num": "129566"})
    assert out["ok"] and out["attached"] == [{"token": "7", "header_token": "31", "note_type": "0", "title": "Customer Requirements",
                                               "created": "9999-01-01", "text_length": 11}]
    assert "secret text" not in str(out) and "Customer Requirements" not in out["missing_titles"] and len(out["missing_titles"]) == 4
    assert [u["token"] for u in out["unlinked_recent"]] == ["8"] and s.writes() == []


def test_probe_notes_refuses_bad_input_and_an_unknown_order_and_is_reachable_from_the_handler(monkeypatch):
    from fakes import FakeSoap

    assert probe.probe_notes(FakeSoap(), {"co_num": "1; drop"})["ok"] is False
    assert probe.probe_notes(FakeSoap(), {"co_num": "5"})["ok"] is False
    s = FakeSoap()
    s.add_record("SLCos", CoNum="         5", RowPointer="RP")
    monkeypatch.setattr(lambda_function, "_client_factory", lambda: s)
    assert lambda_function.lambda_handler({"action": "probe_notes", "co_num": "5"}, None)["ok"]
