import pytest

from fakes import FakeJobsTable
from syteline.eventstore import STALE_AFTER, EventStore
from syteline.reservations import ReservationStore


@pytest.fixture
def table():
    return FakeJobsTable()


def test_the_first_claim_wins_and_the_second_does_not(table):
    events = EventStore(table)
    assert events.claim("d.pdf", "exec-1") is True
    assert events.claim("d.pdf", "exec-2") is False
    assert events.get("d.pdf")["execution"] == "exec-1" and events.get("d.pdf")["state"] == "processing"


def test_a_released_claim_can_be_taken_again(table):
    events = EventStore(table)
    events.claim("d.pdf", "exec-1")
    events.release("d.pdf")
    assert events.get("d.pdf") is None and events.claim("d.pdf", "exec-2") is True


def test_a_stale_claim_of_a_run_that_died_is_taken_over_once(table):
    now = [1000]
    events = EventStore(table, clock=lambda: now[0])
    events.claim("d.pdf", "exec-1")
    now[0] += STALE_AFTER - 1
    assert events.claim("d.pdf", "exec-2") is False  # still recent
    now[0] += 2
    assert events.claim("d.pdf", "exec-2") is True and events.get("d.pdf")["execution"] == "exec-2"


def test_a_stale_claim_that_has_a_reservation_is_never_taken_over(table):
    now = [1000]
    events = EventStore(table, clock=lambda: now[0])
    events.claim("d.pdf", "exec-1")
    events.record("d.pdf", "reserved", "rid-1", state="reserved")
    now[0] += STALE_AFTER * 10
    assert events.claim("d.pdf", "exec-2") is False


def test_record_stores_only_identifiers_states_and_codes(table):
    events = EventStore(table, clock=lambda: 1000)
    events.claim("d.pdf", "exec-1")
    events.record("d.pdf", "save_accepted", "rid-1", state="save_accepted")
    item = events.get("d.pdf")
    assert set(item) == {"pk", "document_id", "execution", "state", "claimed_at", "updated_at", "outcome",
                         "reservation_id", "ttl"}
    assert item["state"] == "save_accepted" and item["ttl"] > 1000


def test_recording_for_a_document_without_a_claim_does_nothing(table):
    EventStore(table).record("none.pdf", "x")
    assert table.items == {}


def test_every_reservation_writes_the_document_marker_and_a_duplicate_does_not(table):
    store = ReservationStore(table, clock=lambda: 1000)
    req = {"request_ref": "r1", "document_id": "d.pdf", "line_count": 2}
    item, dup = store.create_reservation(req, "h", "fp", "jdoe", "arn")
    assert not dup and table.items["DOCRES#d.pdf"]["reservation_id"] == item["reservation_id"]
    assert store.reservation_for_document("d.pdf")["reservation_id"] == item["reservation_id"]
    again, dup = store.create_reservation(req, "h", "fp", "jdoe", "arn")
    assert dup and store.reservation_for_document("d.pdf")["reservation_id"] == item["reservation_id"]
    assert store.reservation_for_document("other.pdf") is None
