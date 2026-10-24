"""Does the mock still match the deployment? (FR-008, FR-012)

`check(run_dir)` compares what the template says now with what the run directory was prepared from, and
`undocumented_actions()` compares the function's actions with the list in docs/MOCK_ENVIRONMENT.md. Both return plain
problem strings; an empty list means they agree.
"""

import json
import os
import re
import sys

import render_env

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))


def check(run_dir, template=None):
    now = render_env.render(template=template)
    with open(os.path.join(run_dir, "rendered.json")) as f:
        prepared = json.load(f)
    with open(os.path.join(run_dir, "function.env")) as f:
        function_env = dict(line.split("=", 1) for line in f.read().splitlines() if "=" in line)
    problems = []
    for name in sorted(set(now["env"]) - set(function_env)):
        problems.append(f"setting {name} is in the template but not in the function's environment")
    for name in sorted(set(now["env"]) & set(function_env)):
        if now["env"][name] != function_env[name] and name not in prepared_overrides():
            problems.append(f"setting {name} differs: template {now['env'][name]!r}, mock {function_env[name]!r}")
    if now["timeout"] != prepared["timeout"]:
        problems.append(f"timeout differs: template {now['timeout']}, mock {prepared['timeout']}")
    if now["memory"] != prepared["memory"]:
        problems.append(f"memory differs: template {now['memory']}, mock {prepared['memory']}")
    for m, path in sorted(set(map(tuple, now["routes"])) ^ set(map(tuple, prepared["routes"]))):
        problems.append(f"route {m} {path} differs between the template and the mock")
    if _canon(now["statements"]) != _canon(prepared["statements"]):
        problems.append("the function role's permissions differ between the template and the mock")
    return problems


def prepared_overrides():
    from edge import prepare

    return set(prepare.OVERRIDES)


def _canon(statements):
    return sorted(json.dumps(s, sort_keys=True) for s in statements)


def function_actions():
    import lambda_function

    return sorted(set(lambda_function.ACTIONS) | set(lambda_function.WORK_ACTIONS) | {lambda_function.DEFAULT_ACTION})


def undocumented_actions():
    with open(os.path.join(ROOT, "docs", "MOCK_ENVIRONMENT.md")) as f:
        text = f.read()
    return [a for a in function_actions() if not re.search(rf"`{re.escape(a)}`", text)]
