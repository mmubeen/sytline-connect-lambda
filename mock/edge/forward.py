"""Forward a request the edge has allowed to the Moto server behind it."""

import requests

_DROP = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}


class MotoForward:
    def __init__(self, moto_url):
        self.base = moto_url.rstrip("/")

    def __call__(self, method, path, headers, query, body):
        h = {k: v for k, v in headers.items() if k.lower() not in _DROP}
        r = requests.request(method, self.base + path, headers=h, data=body, timeout=60, allow_redirects=False)
        out = {k: v for k, v in r.headers.items() if k.lower() not in ("content-length", "transfer-encoding",
                                                                       "content-encoding", "connection")}
        return r.status_code, out, r.content
