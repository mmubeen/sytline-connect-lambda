"""`edge.prepare`: what `make -C mock up` writes into the git-ignored run directory before the containers start."""

import json
import os
import stat

import pytest

from edge import prepare


@pytest.fixture
def run(tmp_path):
    d = tmp_path / "run"
    prepare.prepare(str(d))
    return d


def test_function_env_has_the_template_settings_and_the_mock_overrides(run):
    env = dict(line.split("=", 1) for line in (run / "function.env").read_text().splitlines())
    assert env["JOBS_TABLE"] == "sytline-connect-JobsTable"
    assert env["AWS_ENDPOINT_URL"] == "http://edge:4566"
    assert env["AWS_USE_FIPS_ENDPOINT"] == "false"            # botocore refuses FIPS together with a custom endpoint
    assert env["SYTELINE_LIVE_WRITES"] == "true"               # writes go to the mock ERP only (addresses are checked)
    assert env["AWS_LAMBDA_FUNCTION_NAME"] == "sytline-connect-fn"
    assert env["AWS_LAMBDA_FUNCTION_TIMEOUT"] == "300" and env["AWS_LAMBDA_FUNCTION_MEMORY_SIZE"] == "256"
    assert env["SYTELINE_WSDL"].startswith("http://erp:")
    assert "PYTHONPATH" in env


def test_function_env_holds_no_password(run):
    text = (run / "function.env").read_text()
    pw = json.loads((run / "passwords.json").read_text())
    assert pw["SYTELINE_PASS"] not in text and pw["SQL_PASS"] not in text and "SYTELINE_PASS=" not in text


def test_rendered_json_carries_the_statements_and_routes(run):
    r = json.loads((run / "rendered.json").read_text())
    assert r["function_name"] == "sytline-connect-fn" and r["statements"] and r["routes"]


def test_secret_files_are_private_and_not_in_the_env_files(run):
    for name in ("passwords.json", "sql.env"):
        assert stat.S_IMODE(os.stat(run / name).st_mode) == 0o600
    assert "MSSQL_SA_PASSWORD=" in (run / "sql.env").read_text()


def test_preparing_again_keeps_the_passwords(tmp_path):
    prepare.prepare(str(tmp_path))
    first = (tmp_path / "passwords.json").read_text()
    prepare.prepare(str(tmp_path))
    assert (tmp_path / "passwords.json").read_text() == first


def test_every_address_in_the_env_is_a_mock_address(run):
    env = dict(line.split("=", 1) for line in (run / "function.env").read_text().splitlines())
    assert prepare.foreign_addresses(env) == []
    env["SYTELINE_WSDL"] = "http://erp.plant.example/IDORequestService/IDOWebService.asmx?wsdl"
    assert prepare.foreign_addresses(env) == ["SYTELINE_WSDL"]
    env["AWS_ENDPOINT_URL"] = "https://sts.us-gov-east-1.amazonaws.com"
    assert "AWS_ENDPOINT_URL" in prepare.foreign_addresses(env)
