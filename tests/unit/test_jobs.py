import pytest

from fakes import FakeJobsTable
from syteline import jobs
from syteline.jobs import JobStore, RefConflict

REQ = {"request_ref": "r1", "customer": {"name": "ACME"}}


def mk(clock=None):
    t = FakeJobsTable()
    return JobStore(t, clock=clock or (lambda: 1000)), t


def test_atomic_create_with_ref_index_and_fields():
    s, t = mk()
    job, dup = s.create_job(REQ, "h1", "jdoe", "arn:me")
    assert dup is False and job["state"] == "accepted" and job["uploaded_by"] == "jdoe"
    assert f"REF#r1" in t.items and t.items["REF#r1"]["job_id"] == job["job_id"]
    assert s.get_job(job["job_id"])["caller_identity"] == "arn:me"


def test_same_ref_same_hash_returns_existing_job():
    s, _ = mk()
    a, _ = s.create_job(REQ, "h1", "u", "i")
    b, dup = s.create_job(REQ, "h1", "u", "i")
    assert dup is True and b["job_id"] == a["job_id"]


def test_same_ref_different_hash_conflicts():
    s, _ = mk()
    s.create_job(REQ, "h1", "u", "i")
    with pytest.raises(RefConflict):
        s.create_job(REQ, "OTHER", "u", "i")


def test_only_accepted_can_be_claimed_once():
    s, _ = mk()
    j, _ = s.create_job(REQ, "h", "u", "i")
    assert s.claim(j["job_id"]) is True
    assert s.claim(j["job_id"]) is False
    assert s.get_job(j["job_id"])["state"] == "running" and s.get_job(j["job_id"])["attempts"] == 1


def test_terminal_transitions_and_ttl():
    s, _ = mk()
    j, _ = s.create_job(REQ, "h", "u", "i")
    s.claim(j["job_id"])
    assert s.succeed(j["job_id"], "100", [{"line": 1, "job": "5000"}], {"cust_num": "C1"})
    got = s.get_job(j["job_id"])
    assert got["state"] == "succeeded" and got["erp_records"]["co_num"] == "100" and got["ttl"] > 1000
    assert s.fail(j["job_id"], "X", "m", "s") is False  # cannot leave a terminal state


def test_needs_reconciliation_is_never_listed_or_changed():
    now = [1000]
    s, _ = mk(lambda: now[0])
    j, _ = s.create_job(REQ, "h", "u", "i")
    s.claim(j["job_id"])
    assert s.park(j["job_id"], "UNCERTAIN_OUTCOME", "m", "create_co", [{"kind": "co", "key": "100"}])
    now[0] += 10000
    assert s.list_accepted_older_than(0) == [] and s.list_running_older_than(0) == []
    assert s.claim(j["job_id"]) is False and s.succeed(j["job_id"], "1", []) is False
    assert s.get_job(j["job_id"])["state"] == jobs.NEEDS_RECONCILIATION


def test_stale_running_listed_for_parking_only_after_threshold():
    now = [1000]
    s, _ = mk(lambda: now[0])
    j, _ = s.create_job(REQ, "h", "u", "i")
    s.claim(j["job_id"])
    assert s.list_running_older_than(300) == []
    now[0] += 301
    assert [x["job_id"] for x in s.list_running_older_than(300)] == [j["job_id"]]
