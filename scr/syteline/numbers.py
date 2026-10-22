"""Reserve CO and job numbers, through the web service or the database depending on the operation's route.

The route is decided per operation from the settings (syteline.routes) before anything is sent; no error on one route
ever starts the call on the other. The database route needs live writes (reserving a number is a change in SyteLine)
and opens ONE connection for the whole reservation, closed by `close()` / the context manager.
"""

from . import config, idos, routes
from .errors import LiveWritesDisabled

_KIND = {"next_co": "co", "next_job": "job"}
_METHOD = {"next_co": idos.NEXT_CO_METHOD, "next_job": idos.NEXT_JOB_METHOD}


class NumberSource:
    def __init__(self, soap, cfg=None):
        self._soap = soap
        self._cfg = cfg if cfg is not None else config.load_config()
        self._db = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def close(self):
        if self._db is not None:
            db, self._db = self._db, None
            db.__exit__(None, None, None)

    def next(self, operation):
        """The next number for "next_co" or "next_job" as a digit string ("" when the web service answers nothing).

        Raises LiveWritesDisabled, IntegrationError(DATABASE_NOT_CONFIGURED), ErpRejected, ErpUncertain, ErpUnreachable.
        """
        kind, name = routes.route(operation)
        if kind == routes.WEBSERVICE:
            out = self._soap.invoke(name, _METHOD[operation], [])
            return str(out[0]).strip() if out else ""
        if not config.live_writes_enabled():  # before any connection is made
            raise LiveWritesDisabled("live ERP writes are disabled (SYTELINE_LIVE_WRITES)")
        if self._db is None:
            from .sqlserver import SqlServer

            db = SqlServer(self._cfg)
            db.__enter__()  # may raise DATABASE_NOT_CONFIGURED / ErpUnreachable: nothing was sent
            self._db = db
        return self._db.next_number(_KIND[operation])

    def next_co(self):
        return self.next("next_co")

    def next_job(self):
        return self.next("next_job")
