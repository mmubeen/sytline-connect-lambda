"""src/invoke.sh: invoke the Lambda with a payload read from a file (stubbed `aws`)."""

import base64
import json
import os
import shutil
import stat
import subprocess

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")
SAMPLE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "sample_event.json")

AWS_STUB = '''#!/usr/bin/env bash
echo "AWS $*" >> "$STUB_LOG"
if [[ "$1" == "--version" ]]; then echo "${STUB_AWS_VERSION:-aws-cli/2.15.0 Python/3.11}"; exit 0; fi
if [[ "$*" == *"lambda invoke"* ]]; then
  [[ "${STUB_INVOKE_FAIL:-}" == "1" ]] && { echo "An error occurred (ResourceNotFoundException)" >&2; exit 254; }
  out="${@: -1}"
  if [[ -n "${STUB_RESPONSE+x}" ]]; then printf '%s' "$STUB_RESPONSE" > "$out"; else printf '%s' '{"outcome": "ignored"}' > "$out"; fi
  if [[ -n "${STUB_META:-}" ]]; then printf '%s' "$STUB_META"; else printf '%s' '{"StatusCode": 200, "ExecutedVersion": "$LATEST"}'; fi
fi
'''


@pytest.fixture
def invoke(tmp_path):
    work = tmp_path / "src"
    work.mkdir()
    shutil.copy(os.path.join(SRC, "invoke.sh"), work / "invoke.sh")
    shutil.copy(os.path.join(SRC, "samconfig.toml"), work / "samconfig.toml")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws = bin_dir / "aws"
    aws.write_text(AWS_STUB)
    aws.chmod(aws.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "log"
    log.write_text("")

    def go(*args, payload=None, env=None, deploy_env=None):
        if payload is not None:
            (tmp_path / "payload.json").write_text(payload if isinstance(payload, str) else json.dumps(payload))
        if deploy_env is not None:
            (work / "deploy.env").write_text(deploy_env)
        full_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(log), **(env or {})}
        resolved = [str(tmp_path / "payload.json") if a == "PAYLOAD" else a for a in args]
        log.write_text("")
        p = subprocess.run(["bash", str(work / "invoke.sh"), *resolved], capture_output=True, text=True, env=full_env, cwd=work)
        return p, log.read_text()

    return go


def invoke_line(calls):
    lines = [l for l in calls.splitlines() if "lambda invoke" in l]
    assert lines, calls
    return lines[0]


def test_the_payload_file_is_sent_and_the_response_is_printed(invoke):
    p, calls = invoke("PAYLOAD", payload={"action": "health"})
    assert p.returncode == 0, p.stderr
    line = invoke_line(calls)
    assert "--function-name sytline-connect-fn" in line and "--region us-gov-east-1" in line
    assert "--payload fileb://" in line and "payload.json" in line and "--cli-binary-format raw-in-base64-out" in line
    assert "status: 200" in p.stdout and '"outcome": "ignored"' in p.stdout
    assert "invoking sytline-connect-fn in us-gov-east-1" in p.stdout


def test_the_function_and_region_and_profile_can_be_chosen(invoke):
    p, calls = invoke("--function", "other-fn", "--region", "us-gov-west-1", "--profile", "govcloud", "PAYLOAD",
                      payload={"action": "health"})
    assert p.returncode == 0, p.stderr
    line = invoke_line(calls)
    assert "--function-name other-fn" in line and "--region us-gov-west-1" in line and "--profile govcloud" in line


def test_the_profile_comes_from_deploy_env_and_the_flag_wins(invoke):
    p, calls = invoke("PAYLOAD", payload={"action": "health"}, deploy_env='AWS_PROFILE_NAME="from-env"\n')
    assert "--profile from-env" in invoke_line(calls)
    p, calls = invoke("--profile", "flag", "PAYLOAD", payload={"action": "health"})
    assert "--profile flag" in invoke_line(calls) and "from-env" not in invoke_line(calls)


def test_cli_v1_gets_no_binary_format_option(invoke):
    p, calls = invoke("PAYLOAD", payload={"action": "health"}, env={"STUB_AWS_VERSION": "aws-cli/1.27.0 Python/3.9"})
    assert p.returncode == 0 and "--cli-binary-format" not in invoke_line(calls)


def test_a_payload_that_is_not_json_is_refused_before_any_call(invoke):
    p, calls = invoke("PAYLOAD", payload="{'single': 'quotes', 'x': None}")
    assert p.returncode == 2 and "not valid JSON" in p.stderr and "lambda invoke" not in calls


@pytest.mark.parametrize("payload", ["[1, 2]", '"text"', "5"])
def test_a_payload_that_is_not_an_object_is_refused(invoke, payload):
    p, calls = invoke("PAYLOAD", payload=payload)
    assert p.returncode == 2 and "must hold a JSON object" in p.stderr and "lambda invoke" not in calls


def test_a_missing_file_or_no_file_or_two_files_or_an_unknown_option_is_refused(invoke, tmp_path):
    for args, text in ((("/nope/x.json",), "not found"), ((), "give the payload file"), (("--bogus", "PAYLOAD"), "unknown option"),
                       (("PAYLOAD", "PAYLOAD"), "only one payload file")):
        p, calls = invoke(*args, payload={"action": "health"})
        assert p.returncode == 2 and text in p.stderr and "lambda invoke" not in calls, args


def test_check_looks_at_the_file_and_invokes_nothing(invoke):
    p, calls = invoke("--check", SAMPLE)
    assert p.returncode == 0 and calls == ""
    assert "top-level keys" in p.stdout and "source='aws.states'" in p.stdout
    assert "detail-type='Step Functions Execution Status Change'" in p.stdout and "detail is dict" in p.stdout


def test_check_says_what_is_wrong_with_a_payload_that_lost_its_detail(invoke):
    p, calls = invoke("--check", "PAYLOAD", payload={"source": "aws.states", "detail-type": "Step Functions Execution Status Change",
                                                    "detail": "{\"status\": \"SUCCEEDED\"}"})
    assert p.returncode == 0 and "detail is str" in p.stdout
    p, calls = invoke("--check", "PAYLOAD", payload={"dry_run": True})
    assert "no action" in p.stdout and "refuse it" in p.stdout  # a bare payload would be refused: say so before sending it
    p, calls = invoke("--check", "PAYLOAD", payload={"action": "health"})
    assert "action: 'health'" in p.stdout


def test_a_function_error_is_shown_and_makes_the_exit_status_1(invoke):
    p, calls = invoke("PAYLOAD", payload={"action": "health"}, env={
        "STUB_META": '{"StatusCode": 200, "FunctionError": "Unhandled"}',
        "STUB_RESPONSE": '{"errorMessage": "boom", "errorType": "KeyError"}'})
    assert p.returncode == 1 and "function error: Unhandled" in p.stdout and '"errorMessage": "boom"' in p.stdout


def test_a_failed_invoke_call_is_reported_and_exits_1(invoke):
    p, calls = invoke("PAYLOAD", payload={"action": "health"}, env={"STUB_INVOKE_FAIL": "1"})
    assert p.returncode == 1 and "the invoke call failed" in p.stderr and "ResourceNotFoundException" in p.stderr


def test_log_prints_the_decoded_log_tail_before_the_response(invoke):
    tail = base64.b64encode(b"START RequestId: abc\nEND RequestId: abc\n").decode()
    p, calls = invoke("--log", "PAYLOAD", payload={"action": "health"},
                      env={"STUB_META": json.dumps({"StatusCode": 200, "LogResult": tail})})
    assert p.returncode == 0 and "--log-type Tail" in invoke_line(calls)
    assert p.stdout.index("START RequestId: abc") < p.stdout.index("--- response ---") < p.stdout.index('"outcome"')


def test_async_sends_an_event_invocation_and_prints_no_body(invoke):
    p, calls = invoke("--async", "--log", "PAYLOAD", payload={"action": "health"},
                      env={"STUB_META": '{"StatusCode": 202}'})
    line = invoke_line(calls)
    assert p.returncode == 0 and "--invocation-type Event" in line and "--log-type" not in line
    assert "status: 202" in p.stdout and "outcome" not in p.stdout


def test_a_response_that_is_not_json_is_printed_as_it_is(invoke):
    p, calls = invoke("PAYLOAD", payload={"action": "health"}, env={"STUB_RESPONSE": "plain text"})
    assert p.returncode == 0 and "plain text" in p.stdout


def test_help_prints_the_usage_without_a_file(invoke):
    p, calls = invoke("--help")
    assert p.returncode == 0 and "PAYLOAD_FILE" in p.stdout and calls == ""


def test_debug_sends_a_copy_of_the_payload_with_debug_true_and_leaves_the_file_alone(invoke, tmp_path):
    p, calls = invoke("--debug", "PAYLOAD", payload={"action": "document_event", "x": 1})
    assert p.returncode == 0, p.stderr
    sent = invoke_line(calls).split("--payload fileb://")[1].split()[0]
    assert "payload.json" not in sent  # a temporary copy, not the original
    assert json.loads((tmp_path / "payload.json").read_text()) == {"action": "document_event", "x": 1}
    p, calls = invoke("PAYLOAD", payload={"action": "health"})
    assert "payload.json" in invoke_line(calls)  # without --debug the original file is sent as it is

