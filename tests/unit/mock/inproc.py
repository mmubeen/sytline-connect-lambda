"""Run the function's own handler in this process against the mock services (no containers).

It stands in for the function container in tests that can run anywhere: the code under test, the settings (from the
template), the ERP mock over HTTP, the edge and Moto are all real; only the interpreter (3.12 here, 3.13 in the
container) and the SQL Server (the web-service routes are used instead) differ.
"""

import json
import threading

import lambda_function


class Context:
    def get_remaining_time_in_millis(self):
        return 300_000

    function_name = "sytline-connect-fn"


class InProcessRuntime:
    def __init__(self):
        self.threads = []
        self.results = []

    def invoke_sync(self, event, timeout=None):
        out = lambda_function.lambda_handler(event, Context())
        return {"status": 200, "function_error": None, "payload": json.loads(json.dumps(out, default=str)),
                "duration_ms": 0}

    def invoke_async(self, event):
        def run():
            try:
                self.results.append(lambda_function.lambda_handler(event, Context()))
            except Exception as exc:  # recorded, so a failing background action is visible to the test
                self.results.append({"exception": f"{type(exc).__name__}: {exc}"})

        t = threading.Thread(target=run, daemon=True)
        self.threads.append(t)
        t.start()

    def wait(self, timeout=30):
        for t in list(self.threads):
            t.join(timeout)
