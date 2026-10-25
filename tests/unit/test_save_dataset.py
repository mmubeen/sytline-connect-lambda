"""SaveDataSet is sent as the real WSDL declares it (updateDataSet = schema + any), not as a string.

The WSDL fixture reproduces the real service's SaveDataSet signature (confirmed from `python -m zeep` against the plant
service). The row layout inside the DataSet is still unconfirmed against the live service."""
import os

import pytest

pytest.importorskip("zeep")
from lxml import etree  # noqa: E402
from zeep import Client  # noqa: E402

from syteline import client as sc  # noqa: E402

WSDL = os.path.join(os.path.dirname(__file__), "data", "ido_web_service_save.wsdl")
NS = {"s": "http://frontstep.com/IDOWebService", "d": sc.DIFFGR, "x": sc.XS, "m": sc.MSDATA}


def message(dataset):
    sc._enable_dataset_render()
    c = Client(WSDL)
    return c.create_message(c.service, "SaveDataSet", "tok", dataset, *sc.SAVE_TAIL)


def test_insert_is_a_dataset_with_schema_and_an_inserted_row():
    msg = message(sc._dataset("SLCos", "Insert", {"CoNum": "129548", "CustNum": "7"}))
    body = msg.find(".//s:updateDataSet", NS)
    assert [etree.QName(c).localname for c in body] == ["schema", "diffgram"]
    assert body.xpath("x:schema//x:element[@name='SLCos']//x:element/@name", namespaces=NS) == ["CoNum", "CustNum"]
    row = body.find("d:diffgram/DataSet/SLCos", NS)
    assert row.get(f"{{{sc.DIFFGR}}}hasChanges") == "inserted"
    assert [(c.tag, c.text) for c in row] == [("CoNum", "129548"), ("CustNum", "7")]
    assert msg.findtext(".//s:refreshAfterSave", namespaces=NS) == "true"


def test_update_carries_the_key_in_a_before_row():
    msg = message(sc._dataset("CCT_Coitems", "Update", {"CoNum": "1", "CoLine": 2, "Teardown": "x"}, {"CoNum": "1", "CoLine": 2}))
    changed = msg.find(".//d:diffgram/DataSet/CCT_Coitems", NS)
    before = msg.find(".//d:diffgram/d:before/CCT_Coitems", NS)
    assert changed.get(f"{{{sc.DIFFGR}}}hasChanges") == "modified"
    assert [c.tag for c in changed] == ["CoNum", "CoLine", "Teardown"] and [c.tag for c in before] == ["CoNum", "CoLine"]



def test_method_parameters_are_one_string():
    assert sc._params_xml(["a&b", 0]) == "<Parameters><Parameter>a&amp;b</Parameter><Parameter>0</Parameter></Parameters>"
    assert sc._params_xml([]) == "<Parameters></Parameters>"
