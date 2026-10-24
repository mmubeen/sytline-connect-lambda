"""HTTP client of the ERP mock's control endpoints (/__erp__/...)."""

import requests


class ErpControl:
    def __init__(self, base_url):
        self.base = base_url.rstrip("/") + "/__erp__"

    def _post(self, path, body=None):
        r = requests.post(self.base + path, json=body or {}, timeout=10)
        r.raise_for_status()
        return r.json()

    def reset(self):
        return self._post("/reset")

    def load(self, data):
        return self._post("/load", data)

    def fault(self, fault_id, params=None, remaining=None):
        return self._post("/fault", {"id": fault_id, "params": params or {}, "remaining": remaining})

    def clear_faults(self):
        return self._post("/clear_faults")

    def calls(self, since=0):
        r = requests.get(self.base + "/calls", params={"since": since}, timeout=10)
        r.raise_for_status()
        return r.json()

    def healthy(self):
        try:
            return requests.get(self.base + "/health", timeout=2).ok
        except requests.RequestException:
            return False
