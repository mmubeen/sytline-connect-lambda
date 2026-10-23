"""The ERP-write lease lock (specs/007, research R6, data-model "LOCK#erp-writes")."""

from fakes import FakeJobsTable
from syteline.erplock import LEASE_SECONDS, PK, ErpLock


class Clock:
    def __init__(self, now=1000):
        self.now, self.sleeps = now, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make():
    table, clock = FakeJobsTable(), Clock()
    return ErpLock(table, clock=clock, sleep=clock.sleep), table, clock


def test_the_first_holder_gets_the_lock_with_exactly_the_documented_item():
    lock, table, clock = make()
    assert lock.acquire("req-1", "work_save", 0) is True
    item = table.items[PK]
    assert PK == "LOCK#erp-writes" and LEASE_SECONDS == 330
    assert set(item) == {"pk", "holder", "action", "acquired_at", "expires_at", "ttl"}
    assert item["holder"] == "req-1" and item["action"] == "work_save" and item["acquired_at"] == 1000
    assert item["expires_at"] == 1000 + 330 and item["ttl"] == item["expires_at"] + 3600


def test_a_second_holder_waits_its_budget_then_gives_up():
    lock, table, clock = make()
    lock.acquire("a", "work_save", 0)
    assert lock.acquire("b", "work_upload", 6) is False
    assert clock.sleeps == [2, 2, 2] and table.items[PK]["holder"] == "a"


def test_a_waiting_holder_gets_the_lock_when_the_first_releases():
    lock, table, clock = make()
    lock.acquire("a", "work_save", 0)

    original = clock.sleep

    def sleep_and_release(seconds):
        original(seconds)
        lock.release("a")

    lock._sleep = sleep_and_release
    assert lock.acquire("b", "work_upload", 10) is True and table.items[PK]["holder"] == "b"


def test_a_lock_past_its_lease_is_taken_over_and_the_old_holder_is_no_longer_the_holder():
    lock, table, clock = make()
    lock.acquire("a", "work_save", 0)
    clock.now += LEASE_SECONDS - 1
    assert lock.acquire("b", "work_save", 0) is False and lock.still_holder("a") is True
    clock.now += 1
    assert lock.still_holder("a") is False  # expired: not trusted any more
    assert lock.acquire("b", "work_save", 0) is True and table.items[PK]["holder"] == "b"
    assert lock.still_holder("a") is False and lock.still_holder("b") is True


def test_only_the_holder_releases_it():
    lock, table, clock = make()
    lock.acquire("a", "work_save", 0)
    lock.release("b")  # not ours: nothing happens, no error
    assert table.items[PK]["holder"] == "a"
    lock.release("a")
    assert PK not in table.items
    lock.release("a")  # already gone: no error


def test_a_holder_id_is_stored_at_most_128_characters():
    lock, table, _ = make()
    lock.acquire("x" * 300, "work_save", 0)
    assert len(table.items[PK]["holder"]) == 128 and lock.still_holder("x" * 300)
