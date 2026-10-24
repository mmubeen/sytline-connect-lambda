"""Call records, faults and scenarios of the mock environment (specs/008 data-model.md)."""

import json
import os
import re
import threading
import time

REDACTED = "***"
_SECRET_KEY = re.compile(r"pass|pw|secret|token|authorization|credential|private|pem|apikey|api_key", re.I)

# fault id -> the stand-in it applies to
CATALOGUE = {
    "idp.kms_denied": "aws", "idp.lookup_denied": "aws", "idp.not_found": "aws",
    "erp.unreachable": "erp", "erp.slow": "erp", "erp.row_rejected": "erp", "erp.session_expired": "erp",
    "sql.unreachable": "sql", "sql.login_refused": "sql", "sql.routine_error": "sql", "sql.slow": "sql",
    "secrets.unavailable": "aws", "lock.held": "aws", "lambda.invoke_denied": "aws",
}


class UnknownFault(ValueError):
    pass


def redact(obj):
    """A copy of `obj` with the value of every secret-looking key replaced."""
    if isinstance(obj, dict):
        return {k: (REDACTED if _SECRET_KEY.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def redact_text(text):
    return re.sub(r"(?i)((?:pass(?:word)?|pwd|secret|token)\s*[=:]\s*)\S+", r"\1" + REDACTED, str(text))


class CallLog:
    def __init__(self):
        self._lock = threading.Lock()
        self._calls = []
        self._seq = 0

    def add(self, service, operation, request=None, **extra):
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "ts": time.time(), "service": service, "operation": operation,
                   "caller": None, "action": None, "resource": None, "allowed": True, "fault": None, "status": None,
                   "request_summary": redact(request or {})}
            rec.update(extra)
            self._calls.append(rec)
            return rec

    def query(self, service=None, since=0):
        with self._lock:
            return [c for c in self._calls if c["seq"] > since and (service is None or c["service"] == service)]

    def clear(self):
        with self._lock:
            self._calls.clear()
            self._seq = 0


class FaultSet:
    def __init__(self):
        self._lock = threading.Lock()
        self._faults = []

    def add(self, fault_id, params=None, remaining=None):
        if fault_id not in CATALOGUE:
            raise UnknownFault(f"unknown fault {fault_id!r}; known: {', '.join(sorted(CATALOGUE))}")
        if remaining is not None and (not isinstance(remaining, int) or remaining < 1):
            raise ValueError("remaining must be a positive integer or null")
        with self._lock:
            self._faults.append({"id": fault_id, "target": CATALOGUE[fault_id], "params": dict(params or {}),
                                 "remaining": remaining})
        return fault_id

    def take(self, fault_id):
        """The active fault of that id, counting one use; None when there is none."""
        with self._lock:
            for f in self._faults:
                if f["id"] == fault_id:
                    snapshot = dict(f)
                    if f["remaining"] is not None:
                        f["remaining"] -= 1
                        if f["remaining"] == 0:
                            self._faults.remove(f)
                    return snapshot
        return None

    def peek(self, fault_id):
        with self._lock:
            return next((dict(f) for f in self._faults if f["id"] == fault_id), None)

    def active(self):
        with self._lock:
            return [dict(f) for f in self._faults]

    def clear(self, fault_id=None):
        with self._lock:
            keep = [f for f in self._faults if fault_id is not None and f["id"] != fault_id]
            n = len(self._faults) - len(keep)
            self._faults[:] = keep
            return n


def load_scenario(directory):
    """A scenario folder as one dict (data-model.md); the name must match the folder."""
    directory = os.fspath(directory)
    with open(os.path.join(directory, "scenario.json")) as f:
        sc = json.load(f)
    if sc.get("name") != os.path.basename(directory.rstrip("/")):
        raise ValueError(f"scenario name {sc.get('name')!r} does not match its folder")
    for key in ("idp", "erp", "secrets"):
        with open(os.path.join(directory, key + ".json")) as f:
            sc[key] = json.load(f)
    with open(os.path.join(directory, "sql.sql")) as f:
        sc["sql"] = f.read()
    return sc
