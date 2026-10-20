"""SC-004 with fakes: 20 concurrent intakes -> 20 uploads, processed serially -> 20 distinct numbers.

The fakes cannot prove SyteLine's own number allocation is collision-free under real load; that, and
SC-001 timing, can only be verified in an approved live test (quickstart step 5).
"""

import json
from concurrent.futures import ThreadPoolExecutor

from fakes import FakeJobsTable, FakeLambda, FakeSoap
from handlers import intake, worker
from syteline.jobs import JobStore

CUST = [{"cust_num": "C1", "cust_seq": 0, "name": "ACME"}]


def test_twenty_concurrent_requests_get_distinct_numbers(monkeypatch, live_writes):
    store, lam = JobStore(FakeJobsTable()), FakeLambda()
    soap = FakeSoap(customers=CUST)
    monkeypatch.setattr(intake, "_store_factory", lambda: store)
    monkeypatch.setattr(intake, "_lambda_factory", lambda: lam)
    monkeypatch.setattr(worker, "_store_factory", lambda: store)
    monkeypatch.setattr(worker, "_clients_factory", lambda: (soap, {}))
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "w")

    def submit(i):
        body = {"request_ref": f"ref-{i}", "confirmed": True, "uploaded_by": "u", "customer": {"name": "ACME"},
                "lines": [{"item": "A", "quantity": 1, "uom": "EA"}, {"item": "B", "quantity": 2, "uom": "EA"}]}
        r = intake.handler({"body": json.dumps(body), "requestContext": {"identity": {"userArn": "a"}}}, None)
        return json.loads(r["body"])["upload_id"]

    with ThreadPoolExecutor(20) as pool:
        ids = list(pool.map(submit, range(20)))
    assert len(set(ids)) == 20 and len(lam.invocations) == 20
    for uid in ids:  # the worker runs with reserved concurrency 1: serially
        assert worker.handler({"upload_id": uid}, None) == {"state": "succeeded"}
    done = [store.get_upload(u) if hasattr(store, "get_upload") else store.get_job(u) for u in ids]
    assert len({d["erp_records"]["co_num"] for d in done}) == 20
    jobs = [l["job"] for d in done for l in d["erp_records"]["lines"]]
    assert len(jobs) == 40 and len(set(jobs)) == 40
