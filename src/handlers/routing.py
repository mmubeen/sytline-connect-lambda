"""Routing of the one function (specs/007, research R1, R2, R7): API routes, and the time budget of each action."""

import importlib
import time

# (HTTP method, API Gateway resource) -> handler module under `handlers`
ROUTES = {
    ("POST", "/confirmed-orders"): "intake",
    ("GET", "/upload-jobs/{id}"): "status",
    ("POST", "/job-reservations"): "reserve",
    ("POST", "/job-reservations/{id}/save"): "save_intake",
    ("GET", "/job-reservations/{id}"): "reservation_status",
}

# seconds an action may use; API actions stop before API Gateway gives up, the background actions use the real time left
BUDGETS = {
    "reserve": 25, "intake": 12, "save_intake": 12, "status": 8, "reservation_status": 8,
    "health": 55, "add_note": 55, "get_notes": 55, "document_event": 55,
}


def is_api_event(event):
    return isinstance(event, dict) and bool(event.get("httpMethod")) and bool(event.get("resource"))


def find_route(event):
    """The handler module name for an API Gateway event, or None for a route this function does not serve."""
    if not is_api_event(event):
        return None
    return ROUTES.get((str(event["httpMethod"]).upper(), event["resource"]))


def load(module):
    return importlib.import_module(f"handlers.{module}")


class Budget:
    """A context whose remaining time is capped: min(the real remaining time, budget - elapsed), never negative."""

    def __init__(self, context, seconds, clock=time.monotonic):
        self._context, self._seconds, self._clock = context, seconds, clock
        self._start = clock()

    def get_remaining_time_in_millis(self):
        left = int((self._seconds - (self._clock() - self._start)) * 1000)
        real = getattr(self._context, "get_remaining_time_in_millis", None)
        if callable(real):
            left = min(left, real())
        return max(0, left)

    def __getattr__(self, name):  # request id, function name, ... come from the real context
        return getattr(self._context, name)
