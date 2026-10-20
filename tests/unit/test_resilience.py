import pytest

from syteline.client import ErpRejected, ErpUncertain, ErpUnreachable, SoapIdoClient, parse_rows, row_error
from syteline.errors import LiveWritesDisabled


class Fault(Exception):
    pass


Fault.__name__ = "Fault"

OK_ROW = "<r><CoNum>1</CoNum></r>"


class Svc:
    def __init__(self, load=(), save=()):
        self.tokens = 0
        self.load_results, self.save_results = list(load), list(save)

    def CreateSessionToken(self, u, p, c):
        self.tokens += 1
        return f"tok{self.tokens}"

    def _next(self, q):
        r = q.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def LoadDataSet(self, token, *a):
        return self._next(self.load_results)

    def SaveDataSet(self, token, *a):
        return self._next(self.save_results)


def client(svc):
    class Z:
        service = svc

    return SoapIdoClient({"SYTELINE_USER": "u", "SYTELINE_PASS": "p", "SYTELINE_CONFIG": "C"}, zeep_client=Z())


def test_relogin_once_on_expired_session():
    svc = Svc(load=[Fault("Session token expired"), OK_ROW])
    assert client(svc).load("SLCos", ["CoNum"]) == [{"CoNum": "1"}]
    assert svc.tokens == 2


def test_second_expiry_is_not_retried_forever():
    svc = Svc(load=[Fault("session token expired"), Fault("session token expired")])
    with pytest.raises(ErpRejected):
        client(svc).load("SLCos", ["CoNum"])
    assert svc.tokens == 2


def test_load_transport_error_is_unreachable():
    with pytest.raises(ErpUnreachable):
        client(Svc(load=[ConnectionError("down")])).load("SLCos", ["CoNum"])


def test_insert_refused_when_live_writes_off():
    svc = Svc(save=[OK_ROW])
    with pytest.raises(LiveWritesDisabled):
        client(svc).insert("SLCos", {"CoNum": "1"})
    assert svc.save_results == [OK_ROW]  # SaveDataSet never called


def test_insert_timeout_is_uncertain(live_writes):
    with pytest.raises(ErpUncertain):
        client(Svc(save=[TimeoutError("read timed out")])).insert("SLCos", {"CoNum": "1"})


def test_insert_connect_failure_is_definitely_not_sent(live_writes):
    with pytest.raises(ErpUnreachable):
        client(Svc(save=[ConnectionError("Failed to establish a new connection")])).insert("SLCos", {"CoNum": "1"})


def test_insert_fault_is_rejected(live_writes):
    with pytest.raises(ErpRejected):
        client(Svc(save=[Fault("Item not found")])).insert("SLCos", {"CoNum": "1"})


def test_http_200_with_row_level_rejection_is_rejected(live_writes):
    diffgram = ('<diffgram xmlns:diffgr="urn:schemas-microsoft-com:xml-diffgram-v1"><DataSet>'
                '<SLCos diffgr:hasErrors="true" diffgr:Error="Customer is on credit hold"><CoNum>1</CoNum></SLCos>'
                '</DataSet></diffgram>')
    with pytest.raises(ErpRejected, match="credit hold"):
        client(Svc(save=[diffgram])).insert("SLCos", {"CoNum": "1"})


def test_row_error_variants_and_clean_response():
    assert row_error("<a><Error>bad</Error></a>") == "bad"
    assert row_error("<a><b Error='nope'/></a>") == "nope"
    assert row_error(OK_ROW) is None and row_error("") is None


def test_no_delete_operation_exists():
    assert not hasattr(SoapIdoClient, "delete")


def test_parse_rows_handles_dataset_shape():
    xml = "<DataSet><SLCos><CoNum>5</CoNum><Stat>O</Stat></SLCos><SLCos><CoNum>6</CoNum><Stat>C</Stat></SLCos></DataSet>"
    assert parse_rows(xml) == [{"CoNum": "5", "Stat": "O"}, {"CoNum": "6", "Stat": "C"}]


class CallSvc(Svc):
    def __init__(self, call=()):
        super().__init__()
        self.call_results = list(call)

    def CallMethod(self, token, *a):
        return self._next(self.call_results)


def test_invoke_refused_when_live_writes_off():
    svc = CallSvc(call=[["100"]])
    with pytest.raises(LiveWritesDisabled):
        client(svc).invoke("SLCos", "NextCoSp", [])
    assert svc.call_results == [["100"]]  # CallMethod never called


def test_invoke_returns_output_values(live_writes):
    assert client(CallSvc(call=[["100", ""]])).invoke("SLCos", "NextCoSp", []) == ["100", ""]


def test_invoke_fault_is_rejected(live_writes):
    with pytest.raises(ErpRejected):
        client(CallSvc(call=[Fault("no such method")])).invoke("SLCos", "NextCoSp", [])


def test_invoke_timeout_is_uncertain_connect_failure_is_not(live_writes):
    with pytest.raises(ErpUncertain):
        client(CallSvc(call=[TimeoutError("read timed out")])).invoke("SLCos", "NextCoSp", [])
    with pytest.raises(ErpUnreachable):
        client(CallSvc(call=[ConnectionError("Failed to establish a new connection")])).invoke("SLCos", "NextCoSp", [])
