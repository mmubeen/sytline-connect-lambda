"""The ONLY module that talks to the SyteLine database (SQL Server). Specs: specs/005-sql-server-calls.

It exists for the three calls the web service cannot do (see syteline.routes): reserving CO / job numbers by running
SyteLine's own numbering routines, searching the customer address data, and reading a customer's most recent order
codes. Rules enforced here, not left to callers:

  * the connection is ALWAYS TLS with a verified certificate chain; there is no unencrypted connection: without
    `SQL_CA_PEM` nothing connects (`DATABASE_NOT_CONFIGURED`). Only the host-name check can be switched off
    (SYTELINE_SQL_VERIFY_HOSTNAME=false), and every such connection logs a warning;
  * only the statements below can be run (checked before every execute): two SELECTs, the two numbering routines in
    one batch, COMMIT / ROLLBACK and `SELECT 1`. Every value is a parameter. Nothing here inserts, updates or deletes
    a row; the only change is the numbering routines' own (committed) reservation of a number;
  * the login and password never appear in a message, log line or exception text.

Error meaning (research.md R3): a failure to connect or log in, or before a batch was sent -> ErpUnreachable (nothing
happened); a database error from the routine -> rolled back, ErpRejected; a lost connection, timeout or any other
error after the batch was sent or during the commit -> ErpUncertain (a number may be consumed; never retried).
The driver (python-tds) is imported lazily in `_connect`; errors are recognised by class name so tests need not
install it.
"""

import logging
import os
import re
import tempfile

from . import config, errors
from .errors import ErpRejected, ErpUncertain, ErpUnreachable, IntegrationError

log = logging.getLogger(__name__)

ROUTINES = {"co": "NextCoSp", "job": "NextWOSp"}

# -- the whitelist -------------------------------------------------------------------------------
NUMBER_BATCH = ("SET NOCOUNT ON;\nBEGIN TRANSACTION;\nDECLARE @Key AS LongList;\nDECLARE @Infobar AS Infobar;\n"
                "EXEC [dbo].[{routine}] NULL, NULL, %s, @Key OUTPUT, @Infobar OUTPUT;\n"
                "SELECT @Key AS key_val, @Infobar AS infobar;")
COMMIT = "COMMIT TRANSACTION"
ROLLBACK = "IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION"
PING = "SELECT 1"
CANDIDATES = ("SELECT TOP {cap} cust_num, cust_seq, ISNULL(name, '') AS name, ISNULL([addr##1], '') AS addr1, "
              "ISNULL([addr##2], '') AS addr2, ISNULL([addr##3], '') AS addr3, ISNULL([addr##4], '') AS addr4, "
              "ISNULL(city, '') AS city, ISNULL(state, '') AS state, ISNULL(zip, '') AS zip, "
              "ISNULL(country, '') AS country FROM custaddr WHERE {where} ORDER BY cust_num, cust_seq")
LATEST_SAME_SHIP_TO = ("SELECT TOP 1 terms_code, ship_code FROM co WHERE LTRIM(RTRIM(cust_num)) = %s AND cust_seq = %s "
                       "ORDER BY co_num DESC")
LATEST_ANY = "SELECT TOP 1 terms_code, ship_code FROM co WHERE LTRIM(RTRIM(cust_num)) = %s ORDER BY co_num DESC"

_ALLOWED = (
    re.compile(r"^" + re.escape(NUMBER_BATCH).replace(r"\{routine\}", r"(NextCoSp|NextWOSp)")
               .replace(r"\ ", " ").replace("\\\n", "\n") + r"$"),
    re.compile(r"^SELECT TOP \d+ cust_num, cust_seq, ISNULL\(name, ''\) AS name, .* FROM custaddr WHERE "
               r"(LOWER\(name\) LIKE %s|zip LIKE %s)( OR (LOWER\(name\) LIKE %s|zip LIKE %s))* "
               r"ORDER BY cust_num, cust_seq$", re.S),
    re.compile("^" + re.escape(LATEST_SAME_SHIP_TO) + "$"),
    re.compile("^" + re.escape(LATEST_ANY) + "$"),
    re.compile("^" + re.escape(COMMIT) + "$"),
    re.compile("^" + re.escape(ROLLBACK) + "$"),
    re.compile("^" + re.escape(PING) + "$"),
)

_KEY_INT = re.compile(r"(\d+)")  # LongList values come back as "123", "^123^", "^123,124^": the first integer is ours


def _allowed(sql):
    return any(p.match(sql) for p in _ALLOWED)


def _names(exc):
    return {c.__name__ for c in type(exc).__mro__}


def _connect(**kw):
    """The one place the driver is imported (tests replace this)."""
    import pytds

    return pytds.connect(**kw)


class SqlServer:
    """One database connection for one request: `with SqlServer(cfg) as db: db.next_number("co")`."""

    def __init__(self, cfg):
        self._cfg = cfg
        self._conn = None
        self._ca_path = None

    # -- connection ---------------------------------------------------------------------------
    def _scrub(self, text):
        out = str(text)
        for secret in (self._cfg.get("SQL_PASS"), self._cfg.get("SQL_USER")):
            if secret:
                out = out.replace(str(secret), "***")
        return out

    def __enter__(self):
        missing = config.sql_missing(self._cfg)
        if missing:  # refused before any network traffic; names only
            raise IntegrationError(errors.DATABASE_NOT_CONFIGURED,
                                   "database settings missing: " + ", ".join(missing), step="database")
        login_timeout, timeout = config.sql_timeouts()
        verify_host = config.sql_verify_hostname()
        if not verify_host:
            log.warning("database connection with the host-name check OFF (SYTELINE_SQL_VERIFY_HOSTNAME=false); "
                        "the certificate chain is still verified")
        fd, self._ca_path = tempfile.mkstemp(prefix="sqlca-", suffix=".pem")  # mode 0600
        with os.fdopen(fd, "w") as fh:
            fh.write(str(self._cfg["SQL_CA_PEM"]))
        try:
            self._conn = _connect(server=self._cfg["SQL_SERVER"], database=self._cfg["SQL_DATABASE"],
                                  user=self._cfg["SQL_USER"], password=self._cfg["SQL_PASS"],
                                  port=int(self._cfg.get("SQL_PORT") or 1433), cafile=self._ca_path,
                                  validate_host=verify_host, autocommit=True, login_timeout=login_timeout,
                                  timeout=timeout)
        except Exception as exc:
            self._cleanup()
            raise ErpUnreachable(self._scrub(f"database connection failed: {type(exc).__name__}: {exc}")) from None
        log.info("database connection opened (server=%s database=%s)", self._cfg["SQL_SERVER"],
                 self._cfg["SQL_DATABASE"])
        return self

    def __exit__(self, *exc_info):
        self._cleanup()
        return False

    def _cleanup(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._ca_path:
            try:
                os.remove(self._ca_path)
            except OSError:
                pass
            self._ca_path = None

    def _cursor(self):
        if self._conn is None:
            raise ErpUnreachable("database connection is not open")
        return self._conn.cursor()

    def _run(self, sql, params=()):
        if not _allowed(sql):  # the whitelist: nothing else can ever be sent
            raise RuntimeError("statement is not on the database whitelist")
        cur = self._cursor()
        cur.execute(sql, params)
        return cur

    def _rollback_quietly(self):
        try:
            self._run(ROLLBACK).close()
        except Exception:
            pass

    # -- numbering -----------------------------------------------------------------------------
    def next_number(self, kind):
        """Reserve and commit the next CO ("co") or job ("job") number; returns it as a digit string."""
        routine = ROUTINES[kind]
        batch = NUMBER_BATCH.format(routine=routine)
        try:
            cur = self._run(batch, (config.site_id(),))
            row, infobar = None, None
            while True:
                desc = [d[0] for d in (cur.description or [])]
                if desc and desc[0] == "key_val":
                    row = cur.fetchone()
                    break
                if not getattr(cur, "nextset", lambda: False)():
                    break
        except Exception as exc:
            self._rollback_quietly()
            raise self._classify(exc, routine) from None
        if row is None or row[0] is None:
            self._rollback_quietly()
            raise ErpRejected(f"{routine} returned no number")
        infobar = "" if len(row) < 2 or row[1] is None else str(row[1]).strip()
        if infobar and infobar.lower() != "null":
            log.warning("%s Infobar: %s", routine, self._scrub(infobar))
        found = row[0] if isinstance(row[0], int) else _KEY_INT.search(str(row[0]))
        if found is None:
            self._rollback_quietly()
            raise ErpRejected(f"{routine} returned an unreadable number")
        number = int(found if isinstance(found, int) else found.group(1))
        try:
            self._run(COMMIT).close()
        except Exception as exc:  # the commit may or may not have happened: unknown outcome, never retried
            self._rollback_quietly()
            raise ErpUncertain(self._scrub(f"{routine}: commit outcome unknown ({type(exc).__name__})")) from None
        log.info("%s reserved number %s", routine, number)
        return str(number)

    def _classify(self, exc, routine):
        names = _names(exc)
        message = self._scrub(f"{routine}: {type(exc).__name__}: {exc}")
        if "LoginError" in names:
            return ErpUnreachable(message)
        if names & {"ClosedConnectionError", "TimeoutError", "InterfaceError", "OperationalError", "OSError",
                    "ConnectionError"}:
            return ErpUncertain(message)  # the batch was sent: it may have run
        if "DatabaseError" in names:
            return ErpRejected(message)  # the routine reported an error; rolled back by the caller
        return ErpUncertain(message)

    # -- reads ---------------------------------------------------------------------------------
    def _read(self, sql, params=()):
        try:
            cur = self._run(sql, params)
            cols = [d[0] for d in (cur.description or [])]
            return cols, cur.fetchall()
        except Exception as exc:
            names = _names(exc)
            text = self._scrub(f"database read failed: {type(exc).__name__}: {exc}")
            if "DatabaseError" in names and not names & {"OperationalError", "InterfaceError"}:
                raise ErpRejected(text) from None
            raise ErpUnreachable(text) from None  # a read has no side effect, so a lost connection is just unreachable

    def candidates(self, terms, zip5, cap):
        """Customer address rows whose name contains any of `terms` or whose ZIP starts with `zip5` (capped)."""
        clauses, params = [], []
        for t in terms[:5]:
            clauses.append("LOWER(name) LIKE %s")
            params.append(f"%{t}%")
        if zip5:
            clauses.append("zip LIKE %s")
            params.append(f"{zip5}%")
        if not clauses:
            return []
        cols, rows = self._read(CANDIDATES.format(cap=int(cap), where=" OR ".join(clauses)), tuple(params))
        return [dict(zip(cols, r)) for r in rows]

    def latest_order_codes(self, cust_num, cust_seq):
        """(terms_code, ship_code, source) from the customer's most recent order, or None (no order / no codes).

        Same ship-to first (when the sequence is a whole number), then any order of the customer, as the demo does.
        """
        cust = str(cust_num or "").strip()
        if not cust:
            return None
        seq = str(cust_seq).strip() if cust_seq is not None else ""
        if seq.isdigit():
            try:
                _, rows = self._read(LATEST_SAME_SHIP_TO, (cust, int(seq)))
                codes = _codes(rows)
                if codes:
                    return codes + ("same_ship_to",)
            except ErpRejected as exc:  # a failing first query falls through to the second (the demo does too)
                log.warning("customer defaults (same ship-to) query failed: %s", exc)
        _, rows = self._read(LATEST_ANY, (cust,))
        codes = _codes(rows)
        return codes + ("any_order",) if codes else None

    def ping(self):
        self._read(PING)
        return True


def _codes(rows):
    """(terms, ship) of the first row with at least one non-blank code, else None."""
    if not rows:
        return None
    terms, ship = _trim(rows[0][0]), _trim(rows[0][1])
    return (terms, ship) if terms or ship else None


def _trim(v):
    v = v.strip() if isinstance(v, str) else v
    return v or None
