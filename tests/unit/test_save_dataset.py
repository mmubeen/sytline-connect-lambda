"""SaveDataSet is sent as the real WSDL declares it (updateDataSet = schema + any), not as a string.

The WSDL fixture reproduces the real service's SaveDataSet signature (confirmed from `python -m zeep` against the plant
service). The DataSet is named after the IDO (the service refused "DataSet" as an IDO name); the rest of the row layout is still
unconfirmed against the live service."""
import os

import pytest

pytest.importorskip("zeep")
from lxml import etree  # noqa: E402
from zeep import Client  # noqa: E402

from syteline import client as sc  # noqa: E402
from syteline.client import SoapIdoClient  # noqa: E402

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
    assert body.xpath("x:schema/x:element[@name='SLCos']//x:element[@name='IDO']//x:element/@name", namespaces=NS) == ["CoNum", "CustNum"]
    row = body.find("d:diffgram/SLCos/IDO", NS)
    assert row.get(f"{{{sc.DIFFGR}}}hasChanges") == "inserted"
    assert [(c.tag, c.text) for c in row] == [("CoNum", "129548"), ("CustNum", "7")]
    assert msg.findtext(".//s:refreshAfterSave", namespaces=NS) == "true"


def test_update_carries_the_key_in_a_before_row():
    msg = message(sc._dataset("CCT_Coitems", "Update", {"CoNum": "1", "CoLine": 2, "Teardown": "x"}, {"CoNum": "1", "CoLine": 2}))
    changed = msg.find(".//d:diffgram/CCT_Coitems/IDO", NS)
    before = msg.find(".//d:diffgram/d:before/IDO", NS)
    assert changed.get(f"{{{sc.DIFFGR}}}hasChanges") == "modified"
    assert [c.tag for c in changed] == ["CoNum", "CoLine", "Teardown"] and [c.tag for c in before] == ["CoNum", "CoLine"]



def test_method_parameters_are_one_string():
    assert sc._params_xml(["a&b", 0]) == "<Parameters><Parameter>a&amp;b</Parameter><Parameter>0</Parameter></Parameters>"
    assert sc._params_xml([]) == "<Parameters></Parameters>"


# ---- columns(): the property names an IDO declares, read from the schema of a one-row read -------------------------------
def _soap_response(inner):
    body = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><LoadDataSetResponse xmlns="http://x/">'
            f"<LoadDataSetResult>{inner}</LoadDataSetResult></LoadDataSetResponse></s:Body></s:Envelope>")

    class R:
        content = body.encode()

        def raise_for_status(self):
            pass

    return R()


SCHEMA = ('<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" id="CCT_Coitems"><xs:element name="CCT_Coitems">'
          '<xs:complexType><xs:choice><xs:element name="IDO"><xs:complexType><xs:sequence>'
          '<xs:element name="co_num" type="xs:string"/><xs:element name="item" type="xs:string"/>'
          '</xs:sequence></xs:complexType></xs:element></xs:choice></xs:complexType></xs:element></xs:schema>')


def test_columns_come_from_the_schema_even_when_there_are_no_rows():
    assert sc._schema_columns("<DataSet>" + SCHEMA + "</DataSet>") == ["co_num", "item"]
    kept = sc._dataset_result(_soap_response(SCHEMA + "<diffgram/>"), keep_schema=True)
    dropped = sc._dataset_result(_soap_response(SCHEMA + "<diffgram/>"))
    assert "co_num" in kept and "co_num" not in dropped


def test_columns_asks_with_star_then_empty_and_never_returns_a_value():
    asked = []

    class Z:
        class settings:  # noqa: N801
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class service:  # noqa: N801
            @staticmethod
            def CreateSessionToken(*a):
                return "tok"

            @staticmethod
            def LoadDataSet(token, ido, plist, *rest):
                asked.append(plist)
                return _soap_response(SCHEMA + ('<diffgram><CCT_Coitems><IDO><co_num>SECRET</co_num></IDO></CCT_Coitems></diffgram>'
                                                if plist == "*" else ""))

    c = SoapIdoClient({"SYTELINE_USER": "u", "SYTELINE_PASS": "p", "SYTELINE_CONFIG": "C"}, zeep_client=Z())
    assert c.columns("CCT_Coitems") == ("*", ["co_num", "item"]) and asked == ["*"]


def test_columns_reports_why_each_property_list_was_refused():
    class Z:
        class settings:  # noqa: N801
            def __init__(self, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class service:  # noqa: N801
            @staticmethod
            def CreateSessionToken(*a):
                return "tok"

            @staticmethod
            def LoadDataSet(token, ido, plist, *rest):
                class Fault(Exception):
                    pass

                Fault.__name__ = "Fault"
                raise Fault("star is bad" if plist == "*" else "empty is bad")

    c = SoapIdoClient({"SYTELINE_USER": "u", "SYTELINE_PASS": "p", "SYTELINE_CONFIG": "C"}, zeep_client=Z())
    with pytest.raises(sc.ErpRejected) as ei:
        c.columns("CCT_Coitems")
    assert "'*': star is bad" in str(ei.value) and "'': empty is bad" in str(ei.value)
