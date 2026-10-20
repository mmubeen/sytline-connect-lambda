import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scr"))
sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def _live_writes_off(monkeypatch):
    """Every test starts with live writes OFF and no ambient ERP settings (FR-016)."""
    for k in ("SYTELINE_LIVE_WRITES", "SYTELINE_SECRET_ARN", "SYTELINE_DRY_RUN", "SYTELINE_WSDL",
              "SYTELINE_USER", "SYTELINE_PASS", "SYTELINE_SQL_HOST", "SYTELINE_SQL_PORT",
              "SYTELINE_DEFAULT_WHSE", "SYTELINE_DEFAULT_ORDTYPE"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def live_writes(monkeypatch):
    monkeypatch.setenv("SYTELINE_LIVE_WRITES", "true")
