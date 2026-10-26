"""The property pre-check: a save the ERP would refuse halfway is refused before anything is reserved or written."""

import pytest

from fakes import FakeIdp, FakeJobsTable, FakeSoap, po_fields
from syteline import errors, idos, idp, preflight, reservations as rs, savewf
from syteline.errors import ErpRejected, ErpUnreachable
from syteline.reservations import ReservationStore
from syteline.reserve import run_reserve

DOC = "batch-1/PO-1.pdf"
CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]
CFG = {"SYTELINE_DEFAULT_WHSE": "MAIN", "SYTELINE_DEFAULT_ORDTYPE": "J"}


class StrictSoap(FakeSoap):
    """Refuses a property its IDO does not have, like the service ("error property [X] does not exist in [IDO] IDO.")."""

    def __init__(self, lacks=(), **kw):
        super().__init__(**kw)
        self.lacks, self.asked = set(lacks), []

    def load(self, ido, properties, filter_="", record_cap=0):
        if ido in (idos.CO_IDO, idos.LINE_IDO, idos.LINE_WRITE_IDO, idos.JOB_IDO) and filter_ == "":
            self.asked.append((ido, properties[0]))
            if (ido, properties[0]) in self.lacks:
                raise ErpRejected(f"error property [{properties[0]}] does not exist in [{ido}] IDO.")
        return super().load(ido, properties, filter_, record_cap)


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setenv("SYTELINE_CHECK_PROPERTIES", "true")
    monkeypatch.setattr(preflight, "_verified", set())
    monkeypatch.setattr(idp, "_client_factory", lambda f=FakeIdp().add(DOC, po_fields(2)): f)


def test_the_names_to_check_are_the_names_of_the_rows_the_save_builds():
    base = preflight.needed([{"item": "A", "quantity": 1, "uom": "EA"}])
    assert {"CoNum", "CustNum", "CustSeq", "CustPo", "OrderDate", "Stat", "TermsCode", "ShipCode", "OrigSite"} <= set(base[idos.CO_IDO])
    assert {"co_num", "co_line", "item", "qty_ordered", "u_m", "ref_num", "ref_type"} <= set(base[idos.LINE_WRITE_IDO])
    assert {"CoNum", "CoLine", "Item", "QtyOrdered", "RefNum"} <= set(base[idos.LINE_IDO])  # read back through SLCoitems
    assert {"Job", "OrdType", "Whse", "OrdNum", "OrdLine", "RootJob", "JobDate"} <= set(base[idos.JOB_IDO])
    assert "description" in base[idos.LINE_WRITE_IDO] and "Description" in base[idos.JOB_IDO]  # the item-master description
    assert "due_date" not in base[idos.LINE_WRITE_IDO] and "uf_eng_sn" not in base[idos.JOB_IDO] and idos.UDF_IDO not in base
    more = preflight.needed([{"due_date": "2026-11-01", "engine_sn": "E1", "packing": "Box", "teardown": "2026-11-02"}])
    assert "due_date" in more[idos.LINE_WRITE_IDO] and {"uf_eng_sn", "uf_app"} <= set(more[idos.JOB_IDO])
    assert more[idos.UDF_IDO] == ["RowId", "TableName", "UDFDT1"] and "RowPointer" in more[idos.LINE_IDO]


def test_every_missing_name_is_reported_together_and_nothing_is_reserved(live_writes):
    soap = StrictSoap(customers=CUST, lacks={(idos.LINE_WRITE_IDO, "co_num"), (idos.LINE_WRITE_IDO, "co_line"), (idos.JOB_IDO, "OrdNum")})
    store = ReservationStore(FakeJobsTable(), clock=lambda: 1000)
    out = run_reserve({"request_ref": "r1", "confirmed": True, "uploaded_by": "u", "document_id": DOC}, soap, store, "arn")
    f = out["failure"]
    assert f["code"] == errors.ERP_PROPERTIES_UNKNOWN and f["step"] == "check_properties"
    assert "CCT_Coitems: co_num, co_line" in f["message"] and "CCTJobs: OrdNum" in f["message"] and "probe_properties" in f["message"]
    assert [m["property"] for m in out["missing_properties"][idos.LINE_WRITE_IDO]] == ["co_num", "co_line"]
    assert out["reservation"]["state"] == rs.FAILED and out["reservation"]["job_numbers"] == [] and "consumed_unused" not in out
    assert soap.writes() == [] and not [c for c in soap.calls if c[0] == "invoke"]  # no number was consumed


def test_a_complete_property_set_reserves_as_before_and_is_remembered(live_writes):
    soap = StrictSoap(customers=CUST)
    store = ReservationStore(FakeJobsTable(), clock=lambda: 1000)
    out = run_reserve({"request_ref": "r1", "confirmed": True, "uploaded_by": "u", "document_id": DOC}, soap, store, "arn")
    assert out["reservation"]["state"] == rs.RESERVED and "failure" not in out
    asked = len(soap.asked)
    assert asked > 0
    run_reserve({"request_ref": "r2", "confirmed": True, "uploaded_by": "u", "document_id": DOC}, soap, store, "arn")
    assert len(soap.asked) == asked  # names that passed are not asked again in this container


def test_an_unreachable_erp_is_not_reported_as_a_missing_property(live_writes):
    class Down(StrictSoap):
        def load(self, ido, properties, filter_="", record_cap=0):
            if ido == idos.CO_IDO and filter_ == "":
                raise ErpUnreachable("down")
            return super().load(ido, properties, filter_, record_cap)

    store = ReservationStore(FakeJobsTable(), clock=lambda: 1000)
    out = run_reserve({"request_ref": "r1", "confirmed": True, "uploaded_by": "u", "document_id": DOC}, Down(customers=CUST), store, "arn")
    assert out["failure"]["code"] == errors.ERP_UNREACHABLE and out["failure"]["step"] == "check_properties"


def test_the_check_can_be_switched_off(live_writes, monkeypatch):
    monkeypatch.setenv("SYTELINE_CHECK_PROPERTIES", "false")
    soap = StrictSoap(customers=CUST, lacks={(idos.LINE_WRITE_IDO, "co_num")})
    store = ReservationStore(FakeJobsTable(), clock=lambda: 1000)
    out = run_reserve({"request_ref": "r1", "confirmed": True, "uploaded_by": "u", "document_id": DOC}, soap, store, "arn")
    assert out["reservation"]["state"] == rs.RESERVED and soap.asked == []


def test_a_save_is_refused_before_the_order_header_is_written(live_writes):
    soap = StrictSoap(lacks={(idos.LINE_WRITE_IDO, "co_num")})
    res = {"reservation_id": "res-1", "state": "saving", "co_num": "100", "customer_match": {"cust_num": "C1", "cust_seq": 0},
           "job_numbers": [{"line": 1, "job": "5000"}]}
    req = {"confirmed": True, "uploaded_by": "u", "customer": {"name": "ACME"}, "po_number": "PO-1",
           "lines": [{"item": "A", "quantity": 1, "uom": "EA"}]}
    out = savewf.run_save(res, req, soap, CFG)
    assert out["state"] == "failed" and out["code"] == errors.ERP_PROPERTIES_UNKNOWN and out["step"] == "check_properties"
    assert soap.writes() == [] and "CCT_Coitems: co_num" in out["message"]
