"""The routing table and the per-action time budgets (specs/007, research R1, R7)."""

from handlers import routing


class Ctx:
    def __init__(self, ms):
        self.ms = ms
        self.aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return self.ms


def test_the_budget_caps_the_time_when_the_real_time_is_longer():
    now = [100.0]
    b = routing.Budget(Ctx(300_000), 25, clock=lambda: now[0])
    assert b.get_remaining_time_in_millis() == 25_000
    now[0] += 10
    assert b.get_remaining_time_in_millis() == 15_000


def test_the_real_time_wins_when_it_is_shorter():
    assert routing.Budget(Ctx(3_000), 25, clock=lambda: 0.0).get_remaining_time_in_millis() == 3_000


def test_the_budget_never_goes_below_zero():
    now = [0.0]
    b = routing.Budget(Ctx(300_000), 8, clock=lambda: now[0])
    now[0] = 99
    assert b.get_remaining_time_in_millis() == 0


def test_a_context_without_the_method_gives_the_budget_and_other_attributes_pass_through():
    b = routing.Budget(object(), 12, clock=lambda: 0.0)
    assert b.get_remaining_time_in_millis() == 12_000
    assert routing.Budget(Ctx(1), 5).aws_request_id == "req-1"


def test_every_budget_is_the_documented_value():
    assert routing.BUDGETS == {"reserve": 25, "intake": 12, "save_intake": 12, "status": 8, "reservation_status": 8,
                               "health": 55, "add_note": 55, "get_notes": 55, "document_event": 55}


def api(method, resource):
    return {"httpMethod": method, "resource": resource, "path": resource}


def test_the_five_api_routes_resolve_to_their_handlers():
    assert routing.find_route(api("POST", "/confirmed-orders")) == "intake"
    assert routing.find_route(api("GET", "/upload-jobs/{id}")) == "status"
    assert routing.find_route(api("POST", "/job-reservations")) == "reserve"
    assert routing.find_route(api("POST", "/job-reservations/{id}/save")) == "save_intake"
    assert routing.find_route(api("get", "/job-reservations/{id}")) == "reservation_status"
    for name in set(routing.ROUTES.values()):
        assert callable(routing.load(name).handler)


def test_an_unknown_route_or_a_non_api_event_has_no_route():
    assert routing.find_route(api("DELETE", "/confirmed-orders")) is None
    assert routing.find_route(api("GET", "/nope")) is None
    assert routing.find_route({"action": "health"}) is None and routing.find_route(None) is None
    assert routing.is_api_event(api("GET", "/x")) and not routing.is_api_event({"httpMethod": "GET"})
