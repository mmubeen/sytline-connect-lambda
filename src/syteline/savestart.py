"""Accept a save of a reserved reservation and start the worker (shared by the API and the event intake)."""

import json

from .config import worker_function_name


def accept_and_start(store, lambda_client, reservation_id, params):
    """reserved -> save_accepted, then start the worker asynchronously. False if the reservation was not `reserved`.

    Nothing is recorded when the state change cannot be made (the exception propagates, the reservation is still
    usable). A failed worker start leaves the reservation `save_accepted`; the sweeper re-drives it (no write happened).
    """
    if not store.accept_save(reservation_id, params):
        return False
    try:
        lambda_client.invoke(FunctionName=worker_function_name(), InvocationType="Event",
                             Payload=json.dumps({"reservation_id": reservation_id}).encode("utf-8"))
    except Exception:
        pass
    return True
