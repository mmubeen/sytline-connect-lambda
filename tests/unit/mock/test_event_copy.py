"""T068 (feature 006, FR-016 revised): an oversized logged event reaches a real S3 API (Moto) and the line points at it."""

import json
import os

import boto3
import pytest
from moto.server import ThreadedMotoServer

import render_env
from edge import seed
from syteline import eventlog

R = render_env.render(params={"LogEvents": "true"})


@pytest.fixture(scope="module")
def moto_url():
    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


def test_the_whole_masked_event_is_stored_encrypted_and_the_line_names_it(moto_url, tmp_path, monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y", "AWS_DEFAULT_REGION": "us-gov-east-1"}.items():
        monkeypatch.setenv(k, v)
    run = tmp_path / "run"
    run.mkdir()
    seed.World(R, moto_url, scenarios_dir=os.path.join(os.path.dirname(__file__), "scen"), run_dir=str(run)).reset()
    s3 = boto3.client("s3", endpoint_url=moto_url, region_name="us-gov-east-1")
    monkeypatch.setattr(eventlog, "_s3_factory", lambda: s3)
    bucket = R["env"]["EVENT_COPY_BUCKET"]
    event = {"action": "add_note", "text": "z" * 400_000, "headers": {"Authorization": "AWS4-HMAC-SHA256 Credential=AKIASECRET"}}

    line = eventlog.render(event, {"LOG_EVENTS": "true", "EVENT_COPY_BUCKET": bucket})

    assert len(line.encode()) <= eventlog.MAX_LINE_BYTES
    copy = json.loads(line)["event"]["copy"]
    assert copy["bucket"] == bucket
    obj = s3.get_object(Bucket=bucket, Key=copy["key"])
    stored = json.loads(obj["Body"].read())
    assert stored["text"] == "z" * 400_000 and stored["headers"]["Authorization"] == "***"
    assert obj["ServerSideEncryption"] == "AES256" and obj["ContentType"] == "application/json"
