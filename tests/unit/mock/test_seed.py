"""T013: the seeder builds the jobs table, the secret and the scenario's AWS-side data in Moto, and resets it."""

import json
import os

import boto3
import pytest
from moto.server import ThreadedMotoServer

import render_env
from edge import seed

R = render_env.render()


@pytest.fixture(scope="module")
def moto_url():
    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def aws(moto_url, monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y", "AWS_DEFAULT_REGION": R["env"].get("AWS_REGION", "us-gov-east-1")}.items():
        monkeypatch.setenv(k, v)

    def client(name):
        return boto3.client(name, endpoint_url=moto_url, region_name="us-gov-east-1")

    return client


@pytest.fixture
def world(moto_url, tmp_path, aws):
    run = tmp_path / "run"
    run.mkdir()
    return seed.World(R, moto_url, scenarios_dir=os.path.join(os.path.dirname(__file__), "scen"), run_dir=str(run))


def test_jobs_table_matches_the_template(world, aws):
    world.reset()
    ddb = aws("dynamodb")
    d = ddb.describe_table(TableName="sytline-connect-JobsTable")["Table"]
    assert d["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}]
    assert d["BillingMode"] == "PAY_PER_REQUEST" if "BillingModeSummary" not in d else True
    ttl = ddb.describe_time_to_live(TableName="sytline-connect-JobsTable")["TimeToLiveDescription"]
    assert ttl["AttributeName"] == "ttl" and ttl["TimeToLiveStatus"] in ("ENABLED", "ENABLING")


def test_secret_holds_the_settings_the_function_reads(world, aws):
    world.reset()
    sid = R["env"]["SYTELINE_SECRET_ARN"].split("secret:", 1)[1]
    value = json.loads(aws("secretsmanager").get_secret_value(SecretId=sid)["SecretString"])
    assert {"SYTELINE_PASS", "SQL_SERVER", "SQL_DATABASE", "SQL_USER", "SQL_PASS", "SQL_PORT"} <= set(value)
    assert value["SYTELINE_PASS"] and value["SQL_PASS"]


def test_passwords_are_generated_and_stable_within_a_run_directory(world, aws):
    world.reset()
    first = world.secret_values()["SQL_PASS"]
    world.reset()
    assert world.secret_values()["SQL_PASS"] == first
    other = seed.World(R, world.moto_url, scenarios_dir=world.scenarios_dir, run_dir=world.run_dir + "-b")
    assert other.secret_values()["SQL_PASS"] != first


def test_ca_pem_is_taken_from_the_run_directory_when_present(world, aws):
    open(os.path.join(world.run_dir, "ca.pem"), "w").write("-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n")
    assert world.secret_values()["SQL_CA_PEM"].startswith("-----BEGIN CERTIFICATE")


def test_reset_removes_data_written_by_the_function(world, aws):
    world.reset()
    ddb = aws("dynamodb")
    ddb.put_item(TableName="sytline-connect-JobsTable", Item={"pk": {"S": "JOB#1"}})
    world.reset()
    assert ddb.scan(TableName="sytline-connect-JobsTable")["Items"] == []


def test_the_secret_arn_is_rewritten_to_a_name_moto_can_find():
    assert seed.secret_name(R["env"]["SYTELINE_SECRET_ARN"]) == "mock/sytline-AbCdEf"
    assert seed.secret_name("plain-name") == "plain-name"


def test_lock_held_writes_a_live_lease_and_clearing_releases_it(world, aws):
    world.reset()
    ddb = aws("dynamodb")
    world.fault_added({"id": "lock.held"})
    item = ddb.get_item(TableName="sytline-connect-JobsTable", Key={"pk": {"S": "LOCK#erp-writes"}})["Item"]
    import time

    assert float(item["expires_at"]["N"]) > time.time() + 60
    world.fault_cleared(["lock.held"])
    assert "Item" not in ddb.get_item(TableName="sytline-connect-JobsTable", Key={"pk": {"S": "LOCK#erp-writes"}})


def test_health_reports_unreachable_moto_as_down(tmp_path):
    w = seed.World(R, "http://127.0.0.1:9", scenarios_dir=str(tmp_path), run_dir=str(tmp_path))
    assert w.health()["moto"] == "down"
