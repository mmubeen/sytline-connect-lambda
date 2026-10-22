import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def _live_writes_off(monkeypatch):
    """Every test starts with live writes OFF and no ambient ERP settings (FR-016)."""
    for k in ("SYTELINE_LIVE_WRITES", "SYTELINE_SECRET_ARN", "SYTELINE_DRY_RUN", "SYTELINE_WSDL",
              "SYTELINE_USER", "SYTELINE_PASS", "SYTELINE_DEFAULT_WHSE", "SYTELINE_DEFAULT_ORDTYPE",
              "SYTELINE_CUSTOMER_IDO", "SYTELINE_NEXT_CO_IDO", "SYTELINE_NEXT_JOB_IDO", "SYTELINE_NOTE_METHOD_IDO"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def live_writes(monkeypatch):
    monkeypatch.setenv("SYTELINE_LIVE_WRITES", "true")


@pytest.fixture(scope="module")
def template():
    from cfn_template import load_template

    return load_template()


@pytest.fixture(scope="module")
def standalone_template():
    import os

    from cfn_template import load_template

    return load_template(os.path.join(os.path.dirname(__file__), "..", "src", "template-standalone.yaml"))
