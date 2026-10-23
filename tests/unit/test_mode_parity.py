"""The plant and the stand-alone deployments stay in sync for the settings they share (spec FR-015, SC-011).

A setting is *shared* when a parameter of the same name exists in both templates (computed, not listed), so adding a
setting to both modes puts it under test automatically; adding it to only one mode is never an error. For every shared
setting the two templates, scr/deploy.sh and the two example files must agree (research.md R17 to R19). The checkers are
plain functions returning lists of violation strings so the self-test can prove they notice drift.
"""

import copy
import os
import re

import pytest

from cfn_template import load_template

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SRC = os.path.join(ROOT, "src")
PLANT_TEMPLATE = os.path.join(SRC, "template.yaml")
STANDALONE_TEMPLATE = os.path.join(SRC, "template-standalone.yaml")
SCRIPT = os.path.join(SRC, "deploy.sh")
PLANT_EXAMPLE = os.path.join(SRC, "deploy.env.example")
STANDALONE_EXAMPLE = os.path.join(SRC, "deploy.standalone.env.example")

COMPARED_KEYS = ("Type", "Default", "AllowedValues", "AllowedPattern")  # descriptions describe the mode: they may differ
FUNCTION = "DirectFunction"  # the function both deployments have

# Environment variables that exist (by name) in both deployments but are legitimately wired differently. Each needs a reason,
# and the test fails if an entry stops being necessary.
WIRING_EXCEPTIONS = {
    "SYTELINE_WSDL": "the plant takes one address parameter; the stand-alone takes an https/http pair so CloudFormation "
                     "can require the plain-HTTP override for http:// (feature 003)",
}
# Parameters whose deploy.sh variable is derived rather than read directly: {parameter: (script variable, reason)}
SCRIPT_EXCEPTIONS = {
    "LiveWrites": ("live", "derived from LIVE_WRITES after the approval-reference check"),
}


def snake(param):
    """SqlLoginTimeout -> SQL_LOGIN_TIMEOUT: the deploy.env variable of a stack parameter."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", param).upper()


def shared_parameters(plant, standalone):
    return sorted(set(plant["Parameters"]) & set(standalone["Parameters"]))


def template_violations(plant, standalone):
    out = []
    for name in shared_parameters(plant, standalone):
        for key in COMPARED_KEYS:
            a, b = plant["Parameters"][name].get(key), standalone["Parameters"][name].get(key)
            if a != b:
                out.append(f"templates: {name}: {key} differs (plant {a!r}, stand-alone {b!r})")
    return out


def _env(template):
    """Environment of the function as deployed: the template's Globals for functions plus the function's own."""
    merged = dict((((template.get("Globals") or {}).get("Function") or {}).get("Environment") or {}).get("Variables") or {})
    merged.update(template["Resources"][FUNCTION]["Properties"]["Environment"]["Variables"])
    return merged


def wiring_violations(plant, standalone):
    out, ep, es = [], _env(plant), _env(standalone)
    shared = set(shared_parameters(plant, standalone))
    for var in sorted(set(ep) & set(es)):  # same variable name in both deployments
        if var in WIRING_EXCEPTIONS:
            continue
        if ep[var] != es[var]:
            out.append(f"wiring: {var}: wired differently (plant {ep[var]!r}, stand-alone {es[var]!r})")
    for label, env, other in (("plant", ep, es), ("stand-alone", es, ep)):
        for var, value in sorted(env.items()):  # a shared parameter must feed the same variable in both
            if isinstance(value, dict) and set(value) == {"Ref"} and value["Ref"] in shared and other.get(var) != value:
                out.append(f"wiring: {value['Ref']}: feeds {var} in the {label} template but not in the other")
    for var in sorted(WIRING_EXCEPTIONS):
        if var not in ep or var not in es:
            out.append(f"exceptions: {var}: listed but not present in both templates")
        elif ep[var] == es[var]:
            out.append(f"exceptions: {var}: listed but wired identically (remove it from WIRING_EXCEPTIONS)")
    return out


def _regions(script):
    """(stand-alone branch text, plant overrides text) of deploy.sh."""
    a = script.index("# Stand-alone (no VPC) deployment")
    b = script.index("# --- always required")
    c = script.index("\noverrides=(")  # not attach_overrides=( in the stand-alone branch
    return script[a:b], script[c:script.index("\n)\n", c)]


def _passes(region):
    """{parameter: (expression, variable, default)} for every `q <Param> "<expression>"` in a deploy.sh region."""
    found = {}
    for param, expr in re.findall(r'\(q (\w+) "(.*?)"\)"', region):
        m = re.search(r"\$\{(\w+)(?::-([^}]*))?\}", expr) or re.search(r"\$(\w+)", expr)
        found[param] = (expr, m.group(1) if m else None, m.group(2) if m and m.re.groups > 1 else None)
    return found


def script_violations(script, plant, standalone):
    out = []
    regions = dict(zip(("stand-alone", "plant"), _regions(script)))
    for name in shared_parameters(plant, standalone):
        want_var, why = SCRIPT_EXCEPTIONS.get(name, (snake(name), None))
        default = plant["Parameters"][name].get("Default")
        for label, region in regions.items():
            passed = _passes(region)
            if name not in passed:
                out.append(f"deploy.sh: {name}: not passed in the {label} branch")
                continue
            _, var, given = passed[name]
            if var != want_var:
                out.append(f"deploy.sh: {name}: the {label} branch reads {var!r}, expected {want_var!r}")
            if given is not None and default is not None and given != default:
                out.append(f"deploy.sh: {name}: the {label} branch defaults to {given!r}, the template to {default!r}")
    for name in sorted(SCRIPT_EXCEPTIONS):
        if name not in shared_parameters(plant, standalone):
            out.append(f"exceptions: {name}: listed in SCRIPT_EXCEPTIONS but not a shared parameter")
    return out


def example_violations(examples, plant, standalone):
    """`examples` is {file label: text}."""
    out = []
    for name in shared_parameters(plant, standalone):
        var = snake(name)
        default = plant["Parameters"][name].get("Default")
        for label, text in examples.items():
            m = re.search(rf"^#?\s*{re.escape(var)}=(.*)$", text, re.M)
            if not m:
                out.append(f"{label}: {name}: no {var}= line")
            elif default not in (None, "") and m.group(1).strip().strip("\"'") != default:
                out.append(f"{label}: {name}: {var} is {m.group(1).strip()!r}, the template default is {default!r}")
    return out


# ---- the real files ---------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def files():
    read = lambda p: open(p).read()  # noqa: E731
    return {"plant": load_template(PLANT_TEMPLATE), "standalone": load_template(STANDALONE_TEMPLATE),
            "script": read(SCRIPT), "examples": {"deploy.env.example": read(PLANT_EXAMPLE),
                                                 "deploy.standalone.env.example": read(STANDALONE_EXAMPLE)}}


def no_violations(found):
    assert found == [], "the plant and stand-alone deployments have drifted:\n  " + "\n  ".join(found)


def test_the_shared_set_is_the_expected_one_and_not_silently_empty(files):
    known = {"LiveWrites", "SqlEncrypt", "SqlLoginTimeout", "SqlVerifyHostname", "SytelineConfig", "SytelineSecretArn",
             "SytelineUser", "UseFipsEndpoints"}
    assert known <= set(shared_parameters(files["plant"], files["standalone"]))  # a loader failure cannot pass silently


def test_the_shared_parameters_are_defined_the_same_in_both_templates(files):
    no_violations(template_violations(files["plant"], files["standalone"]))


def test_the_shared_settings_feed_the_same_environment_variables_in_both_templates(files):
    no_violations(wiring_violations(files["plant"], files["standalone"]))


def test_deploy_sh_passes_every_shared_setting_in_both_branches_with_the_templates_default(files):
    no_violations(script_violations(files["script"], files["plant"], files["standalone"]))


def test_both_example_files_list_every_shared_setting_with_the_templates_default(files):
    no_violations(example_violations(files["examples"], files["plant"], files["standalone"]))


def test_every_exception_has_a_reason_and_is_still_needed(files):
    assert all(reason.strip() for reason in WIRING_EXCEPTIONS.values())
    assert all(why.strip() for _, why in SCRIPT_EXCEPTIONS.values())
    # the wiring check reports a stale exception; the script check reports one for a parameter that is no longer shared
    assert [v for v in wiring_violations(files["plant"], files["standalone"]) if v.startswith("exceptions:")] == []
    assert [v for v in script_violations(files["script"], files["plant"], files["standalone"])
            if v.startswith("exceptions:")] == []


# ---- the checker notices drift (SC-011) ---------------------------------------------------------------------------------
def own_env(template):
    return template["Resources"][FUNCTION]["Properties"]["Environment"]["Variables"]


def mutated(files):
    return copy.deepcopy(files["plant"]), copy.deepcopy(files["standalone"])


def test_a_changed_default_in_one_template_is_reported(files):
    plant, standalone = mutated(files)
    standalone["Parameters"]["SqlLoginTimeout"]["Default"] = "9"
    found = template_violations(plant, standalone)
    assert len(found) == 1 and "SqlLoginTimeout" in found[0] and "Default" in found[0]


def test_changed_allowed_values_or_pattern_in_one_template_are_reported(files):
    plant, standalone = mutated(files)
    plant["Parameters"]["SqlEncrypt"]["AllowedValues"] = ["true", "false", "auto"]
    plant["Parameters"]["SqlLoginTimeout"]["AllowedPattern"] = "^[0-9]+$"
    found = template_violations(plant, standalone)
    assert any("SqlEncrypt" in v and "AllowedValues" in v for v in found)
    assert any("SqlLoginTimeout" in v and "AllowedPattern" in v for v in found)


def test_a_description_difference_alone_is_not_drift(files):
    plant, standalone = mutated(files)
    standalone["Parameters"]["LiveWrites"]["Description"] = "worded for this mode"
    assert template_violations(plant, standalone) == []


def test_a_renamed_or_missing_environment_variable_in_one_template_is_reported(files):
    plant, standalone = mutated(files)
    env = own_env(standalone)
    env["SYTELINE_SQL_LOGIN_TIMEOUT_X"] = env.pop("SYTELINE_SQL_LOGIN_TIMEOUT")
    found = wiring_violations(plant, standalone)
    assert any("SqlLoginTimeout" in v and "SYTELINE_SQL_LOGIN_TIMEOUT" in v for v in found)
    plant, standalone = mutated(files)
    own_env(plant)["SYTELINE_LIVE_WRITES"] = "true"  # same name, different wiring
    assert any("SYTELINE_LIVE_WRITES" in v for v in wiring_violations(plant, standalone))


def test_a_stale_wiring_exception_is_reported(files):
    plant, standalone = mutated(files)
    own_env(standalone)["SYTELINE_WSDL"] = _env(plant)["SYTELINE_WSDL"]
    assert any(v.startswith("exceptions: SYTELINE_WSDL") for v in wiring_violations(plant, standalone))


def test_a_setting_missing_from_one_deploy_branch_or_with_another_default_is_reported(files):
    script = files["script"]
    standalone_part, plant_part = _regions(script)
    no_standalone = script.replace(standalone_part, standalone_part.replace(
        '"$(q SqlLoginTimeout "${SQL_LOGIN_TIMEOUT:-5}")" \\\n', ""))
    found = script_violations(no_standalone, files["plant"], files["standalone"])
    assert found == ["deploy.sh: SqlLoginTimeout: not passed in the stand-alone branch"]
    other_default = script.replace(plant_part, plant_part.replace("${SQL_LOGIN_TIMEOUT:-5}", "${SQL_LOGIN_TIMEOUT:-6}"))
    found = script_violations(other_default, files["plant"], files["standalone"])
    assert len(found) == 1 and "plant branch defaults to '6'" in found[0]  # only the branch that was changed
    other_var = script.replace(plant_part, plant_part.replace("${SQL_ENCRYPT:-true}", "${SQLENCRYPT:-true}"))
    assert any("SqlEncrypt" in v and "SQLENCRYPT" in v for v in
               script_violations(other_var, files["plant"], files["standalone"]))


def test_a_variable_missing_from_one_example_or_with_another_value_is_reported(files):
    examples = dict(files["examples"])
    examples["deploy.standalone.env.example"] = re.sub(r"^SQL_LOGIN_TIMEOUT=.*$", "", examples[
        "deploy.standalone.env.example"], flags=re.M)
    found = example_violations(examples, files["plant"], files["standalone"])
    assert found == ["deploy.standalone.env.example: SqlLoginTimeout: no SQL_LOGIN_TIMEOUT= line"]
    examples = dict(files["examples"])
    examples["deploy.env.example"] = examples["deploy.env.example"].replace("SQL_ENCRYPT=true", "SQL_ENCRYPT=false")
    found = example_violations(examples, files["plant"], files["standalone"])
    assert len(found) == 1 and "SqlEncrypt" in found[0]


def test_a_new_setting_added_to_both_templates_is_checked_automatically(files):
    plant, standalone = mutated(files)
    for t in (plant, standalone):
        t["Parameters"]["BrandNewSetting"] = {"Type": "String", "Default": "x"}
    assert "BrandNewSetting" in shared_parameters(plant, standalone)
    found = script_violations(files["script"], plant, standalone) + example_violations(
        files["examples"], plant, standalone)
    assert any("BrandNewSetting" in v and "deploy.sh" in v for v in found)
    assert any("BrandNewSetting" in v and "BRAND_NEW_SETTING" in v for v in found)


def test_a_setting_that_exists_in_only_one_mode_is_never_an_error(files):
    plant, standalone = mutated(files)
    plant["Parameters"]["PlantOnlySetting"] = {"Type": "String", "Default": "x"}
    standalone["Parameters"]["StandaloneOnlySetting"] = {"Type": "String", "Default": "y"}
    assert template_violations(plant, standalone) == [] and wiring_violations(plant, standalone) == []
    assert script_violations(files["script"], plant, standalone) == []
    assert example_violations(files["examples"], plant, standalone) == []
