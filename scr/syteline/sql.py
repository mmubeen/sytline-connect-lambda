"""
SyteLine SQL Server access (python-tds, pure Python).

Reads (`query`) are always allowed. Anything that can change ERP data (`execute_write`, used for
NextCoSp/NextWoSp number allocation and CreateSpecificNoteSp) is refused with LiveWritesDisabled
unless SYTELINE_LIVE_WRITES is on (FR-016). A connection is opened lazily, never at import time or
in a dry run. The password is never logged.
"""

from . import config
from .errors import ErpUncertain, ErpUnreachable, LiveWritesDisabled


class SqlClient:
    def __init__(self, cfg, connect=None):
        self._cfg = cfg
        self._connect = connect
        self._conn = None

    def _open(self):
        if self._conn is None:
            missing = [k for k in ("SYTELINE_SQL_HOST", "SYTELINE_SQL_PORT", "SYTELINE_SQL_DB",
                                   "SYTELINE_SQL_USER", "SYTELINE_SQL_PASS") if not self._cfg.get(k)]
            if missing:
                raise ErpUnreachable(f"missing SQL config: {', '.join(missing)}")
            try:
                if self._connect:
                    self._conn = self._connect(self._cfg)
                else:
                    import pytds

                    self._conn = pytds.connect(
                        self._cfg["SYTELINE_SQL_HOST"], database=self._cfg["SYTELINE_SQL_DB"],
                        user=self._cfg["SYTELINE_SQL_USER"], password=self._cfg["SYTELINE_SQL_PASS"],
                        port=int(self._cfg["SYTELINE_SQL_PORT"]), as_dict=True,
                        timeout=config.timeout(), login_timeout=config.timeout(), autocommit=True)
            except Exception as exc:
                raise ErpUnreachable(f"SQL connection failed: {type(exc).__name__}") from None
        return self._conn

    def _run(self, sql, params):
        cur = self._open().cursor()
        try:
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()] if cur.description else []
        finally:
            cur.close()

    def query(self, sql, params=()):
        """Read-only query. Returns a list of row dicts."""
        try:
            return self._run(sql, params)
        except ErpUnreachable:
            raise
        except Exception as exc:
            raise ErpUnreachable(f"SQL query failed: {type(exc).__name__}") from None

    def execute_write(self, sql, params=()):
        """Run a statement/batch that may change ERP data. Gated by SYTELINE_LIVE_WRITES.

        A failure after the statement was sent is uncertain (it may have executed).
        """
        if not config.live_writes_enabled():
            raise LiveWritesDisabled("live ERP writes are disabled (SYTELINE_LIVE_WRITES)")
        try:
            return self._run(sql, params)
        except ErpUnreachable:
            raise
        except Exception as exc:
            raise ErpUncertain(f"SQL write outcome unknown: {type(exc).__name__}") from None

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
