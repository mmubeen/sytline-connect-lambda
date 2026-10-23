"""Repair `sample_events.json` (a Python-style dump of the IDP state machine's status change event) into valid JSON.

The saved file has single quotes, `None`/`True`, raw line breaks inside quoted text and spaces inside ARNs and times. The
embedded `input` and `output` are JSON text and are kept as text, as EventBridge delivers them (specs/006, research R3).

    python tests/tools/make_sample_event.py [SOURCE]     # default: sample_events.json at the repository root

Writes tests/fixtures/sample_event.json and, when SOURCE is the repository file, rewrites it as valid JSON.
"""

import ast
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_event.json")


def _fix(value):
    if isinstance(value, dict):
        return {k: _fix(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_fix(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?<=:)\s+(?=\d)", "", value)  # "us-gov-east-1: 108375521309" and "23: 02: 29Z"
    return value


def repair(text):
    try:
        return json.loads(text)  # already valid
    except ValueError:
        pass
    data = ast.literal_eval(re.sub(r"\s*\n\s*", " ", text))
    event = _fix(data)
    for key in ("input", "output"):  # the embedded documents must be JSON text
        json.loads(event["detail"][key])
    return event


def main(argv):
    source = argv[1] if len(argv) > 1 else os.path.join(ROOT, "sample_events.json")
    with open(source) as f:
        event = repair(f.read())
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    for path in {FIXTURE, source} if os.path.abspath(source) == os.path.abspath(os.path.join(ROOT, "sample_events.json")) else {FIXTURE}:
        with open(path, "w") as f:
            json.dump(event, f, indent=2)
            f.write("\n")
    print("wrote", FIXTURE)


if __name__ == "__main__":
    main(sys.argv)
