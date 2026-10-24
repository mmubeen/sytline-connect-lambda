"""Control of the SQL Server container as `sa`: provision the stand-in objects, reset them, load a scenario's rows and
switch the `sql.*` faults on and off (specs/008 R6, R13). Only identifiers made of letters, digits and `_` are accepted for
the database and login names; the generated passwords never appear in a log line."""

import os
import re

HERE = os.path.dirname(__file__)
INIT_SQL = os.path.join(HERE, "..", "sqlserver", "init.sql")
_NAME = re.compile(r"^[A-Za-z0-9_]{1,40}$")


def batches(script):
    """The T-SQL batches of a script, split on lines that are only GO."""
    out, current = [], []
    for line in script.splitlines():
        if line.strip().upper() == "GO":
            if "".join(current).strip():
                out.append("\n".join(current).strip())
            current = []
        else:
            current.append(line)
    if "".join(current).strip():
        out.append("\n".join(current).strip())
    return out


def _connect(**kw):
    import pytds

    return pytds.connect(**kw)


class SqlControl:
    def __init__(self, host, port, sa_password, user, password, database, proxy=None, connect=None, cafile=None):
        for name in (user, database):
            if not _NAME.match(name):
                raise ValueError("database and login names may only contain letters, digits and _")
        self._kw = dict(server=host, port=int(port), user="sa", password=sa_password, autocommit=True, login_timeout=5,
                        timeout=60)
        if cafile:  # the engine requires encryption (forceencryption): verify it against this run's CA
            self._kw["cafile"] = cafile
            self._kw["validate_host"] = False  # python-tds' host check needs an older pyOpenSSL; the CA is still verified
        self._connect = connect or _connect
        self.user, self.password, self.database = user, password, database
        self.proxy = proxy

    # -- plumbing --------------------------------------------------------------------------
    def _run(self, statements, database=None):
        conn = self._connect(**self._kw)
        try:
            cur = conn.cursor()
            if database:
                cur.execute(f"USE [{database}]")
            for s in statements:
                cur.execute(s)
            cur.close()
        finally:
            conn.close()

    def healthy(self):
        try:
            conn = self._connect(**self._kw)
        except Exception:
            return False
        try:
            conn.cursor().execute("SELECT 1")
            return True
        except Exception:
            return False
        finally:
            conn.close()

    # -- lifecycle -------------------------------------------------------------------------
    def provision(self):
        with open(INIT_SQL) as f:
            text = f.read()
        text = (text.replace("@SQL_DB@", self.database).replace("@SQL_USER@", self.user)
                .replace("@SQL_PASS@", self.password.replace("'", "''")))
        self._run(batches(text))

    def reset(self):
        self.clear_faults()
        self._run(["DELETE FROM dbo.custaddr", "DELETE FROM dbo.co",
                   "UPDATE dbo.mock_seq SET next_val = CASE name WHEN 'co' THEN 100 ELSE 5000 END"], self.database)

    def run(self, script):
        self._run(batches(script), self.database)

    # -- faults ----------------------------------------------------------------------------
    def fault(self, fault_id, params):
        if fault_id == "sql.unreachable":
            self.proxy.set_mode("refuse")
        elif fault_id == "sql.login_refused":
            self._run([f"ALTER LOGIN [{self.user}] DISABLE"])
        elif fault_id == "sql.routine_error":
            self._run(["IF NOT EXISTS (SELECT 1 FROM dbo.mock_flags WHERE name = 'routine_error') "
                       "INSERT dbo.mock_flags VALUES ('routine_error', '1')"], self.database)
        elif fault_id == "sql.slow":
            delay = int(params.get("delay_ms", 20000))
            self._run(["DELETE FROM dbo.mock_flags WHERE name = 'slow_ms'",
                       f"INSERT dbo.mock_flags VALUES ('slow_ms', '{delay}')"], self.database)

    def clear_faults(self):
        self._run(["DELETE FROM dbo.mock_flags", f"ALTER LOGIN [{self.user}] ENABLE"], self.database)
        if self.proxy:
            self.proxy.set_mode(None)
