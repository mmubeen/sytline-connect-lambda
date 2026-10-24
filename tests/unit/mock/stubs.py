"""Stand-ins for the edge's collaborators in unit tests."""

import json


class StubBackend:
    """What the edge asks of the seeded world: health, reset, scenarios, encryption keys, the lookup function."""

    def __init__(self):
        self.resets = 0
        self.loaded = []
        self.kms = {}           # resource-arn prefix -> key arn
        self.lock_held = False
        self.lookups = []
        self.services = {"moto": "up", "erp": "up", "sql": "up", "fn": "up"}

    def health(self):
        return dict(self.services)

    def reset(self):
        self.resets += 1

    def scenario_names(self):
        return ["passes-review", "needs-review"]

    def load(self, name):
        if name not in self.scenario_names():
            raise KeyError(name)
        self.loaded.append(name)
        return {"detail": {"name": name}}

    def kms_key_for(self, resource):
        return next((k for prefix, k in self.kms.items() if resource.startswith(prefix)), None)

    def fault_added(self, fault):
        if fault["id"] == "lock.held":
            self.lock_held = True

    def fault_cleared(self, ids):
        if "lock.held" in ids:
            self.lock_held = False

    def lookup(self, payload):
        self.lookups.append(payload)
        return {"ok": True}


class StubRuntime:
    def __init__(self):
        self.sync = []
        self.async_ = []
        self.result = {"status": 200, "function_error": None, "payload": {"ok": True}, "duration_ms": 1}

    def invoke_sync(self, event, timeout=None):
        self.sync.append((event, timeout))
        return dict(self.result)

    def invoke_async(self, event):
        self.async_.append(event)


class StubForward:
    """Stands in for Moto behind the edge."""

    def __init__(self):
        self.seen = []

    def __call__(self, method, path, headers, query, body):
        self.seen.append((method, path))
        self.bodies = getattr(self, "bodies", []) + [body]
        return 200, {"Content-Type": "application/json"}, json.dumps({"forwarded": True}).encode()
