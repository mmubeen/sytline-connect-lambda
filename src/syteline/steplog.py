"""One structured log line per database-backed step: type, outcome, duration and the counts that matter (FR-014).

Never carries credentials or customer details: only the operation, the route, an outcome word and counts.
"""

import json
import logging
import time

log = logging.getLogger("syteline.steps")


def now():
    return time.monotonic()


def step(kind, started, outcome, **fields):
    log.info(json.dumps({"type": kind, "route": "database", "outcome": outcome,
                         "ms": int((time.monotonic() - started) * 1000), **fields}, default=str))
