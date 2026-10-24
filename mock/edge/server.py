"""Container entry of the edge:  python -m edge.server

Starts Moto (in this process), the SQL Server TCP front on :1433, the edge/control API on :4566, and in the background waits for
the database, provisions it and puts the world in its starting state. Everything it needs is in MOCK_RUN_DIR (written by
`edge.prepare`) and the environment.
"""

import json
import logging
import os
import threading
import time

from moto.server import ThreadedMotoServer
from werkzeug.serving import run_simple

from . import app, erpcontrol, forward, runtime, seed, sqlcontrol, sqlproxy
from idp.seeder import IdpSeeder

log = logging.getLogger("mock.edge")


def main():
    logging.basicConfig(level=logging.INFO)
    run_dir = os.environ.get("MOCK_RUN_DIR", "/run/mock")
    with open(os.path.join(run_dir, "rendered.json")) as f:
        rendered = json.load(f)
    with open(os.path.join(run_dir, "passwords.json")) as f:
        passwords = json.load(f)
    sa = dict(line.split("=", 1) for line in open(os.path.join(run_dir, "sql.env")).read().splitlines() if "=" in line)

    moto = ThreadedMotoServer(ip_address="127.0.0.1", port=5000, verbose=False)
    moto.start()
    moto_url = "http://127.0.0.1:5000"

    proxy = sqlproxy.SqlProxy("0.0.0.0", 1433, os.environ.get("SQL_HOST", "sqlserver-real"), 1433)
    proxy.start()
    sql = sqlcontrol.SqlControl(os.environ.get("SQL_HOST", "sqlserver-real"), 1433, sa["MSSQL_SA_PASSWORD"], "sytline_mock",
                                passwords["SQL_PASS"], "SyteLine_Mock", proxy=proxy)
    erp = erpcontrol.ErpControl(os.environ.get("ERP_CONTROL_URL", "http://erp:8080"))
    urls = [u for u in os.environ["MOCK_FN_URLS"].split(",") if u]
    rt = runtime.Runtime(urls, default_timeout=rendered["timeout"])
    world = seed.World(rendered, moto_url, os.environ.get("MOCK_SCENARIOS", "/mock/scenarios"), run_dir, erp=erp, sql=sql,
                       runtime_up=rt.healthy, idp_seeder=IdpSeeder(rendered))
    edge = app.Edge(rendered, forward.MotoForward(moto_url), rt, world)

    ready = threading.Event()
    world.ready = ready.is_set
    orig_health = world.health

    def health():
        out = orig_health()
        if "sql" in out and out["sql"] == "up" and not ready.is_set():
            out["sql"] = "starting"  # the engine answers but the stand-in objects are not provisioned yet
        return out

    world.health = health

    def start():
        deadline = time.monotonic() + int(os.environ.get("MOCK_SQL_WAIT", "300"))
        while not sql.healthy():
            if time.monotonic() > deadline:
                log.error("the SQL Server did not become ready in time")
                return
            time.sleep(3)
        sql.provision()
        world.reset()
        ready.set()
        log.info("mock environment ready")

    threading.Thread(target=start, daemon=True).start()
    run_simple("0.0.0.0", 4566, edge, threaded=True)


if __name__ == "__main__":
    main()
