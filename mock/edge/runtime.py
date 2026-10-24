"""Client of the function containers (specs/008 R1): the Lambda runtime interface emulator handles ONE invocation at a time, so
a pool of them stands in for the service's scale-out (the function starts its own background work while a request runs)."""

import json
import socket
import threading
import time
from urllib.parse import urlparse

import requests


class Runtime:
    def __init__(self, urls, default_timeout=300):
        self.urls = list(urls)
        self._busy = {u: threading.Lock() for u in self.urls}
        self._pick = threading.Condition()
        self.default_timeout = default_timeout

    def _acquire(self):
        with self._pick:
            while True:
                for u in self.urls:
                    if self._busy[u].acquire(blocking=False):
                        return u
                self._pick.wait(timeout=0.2)

    def _release(self, url):
        self._busy[url].release()
        with self._pick:
            self._pick.notify()

    def invoke_sync(self, event, timeout=None):
        url = self._acquire()
        start = time.monotonic()
        try:
            r = requests.post(url, data=json.dumps(event), timeout=(timeout or self.default_timeout) + 30)
            try:
                payload = r.json()
            except ValueError:
                payload = r.text
            return {"status": r.status_code, "function_error": r.headers.get("X-Amz-Function-Error"),
                    "payload": payload, "duration_ms": int((time.monotonic() - start) * 1000)}
        except requests.RequestException as exc:
            return {"status": 502, "function_error": "Unreachable",
                    "payload": {"error": f"the function container could not be reached: {type(exc).__name__}"},
                    "duration_ms": int((time.monotonic() - start) * 1000)}
        finally:
            self._release(url)

    def invoke_async(self, event):
        threading.Thread(target=self.invoke_sync, args=(event,), daemon=True).start()

    def healthy(self):
        for u in self.urls:
            p = urlparse(u)
            try:
                socket.create_connection((p.hostname, p.port), timeout=2).close()
            except OSError:
                return False
        return True
