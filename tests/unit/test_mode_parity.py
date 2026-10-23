"""The plant and the stand-alone deployments stay in sync for the settings they share (spec FR-015, SC-011).

A setting is *shared* when a parameter of the same name exists in both templates (computed, not listed), so adding a
setting to both modes puts it under test automatically; adding it to only one mode is never an error. For every shared
setting the two templates, src/deploy.sh and the single example file must agree (research.md R17 to R19, R24). The checkers are
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
EXAMPLE = os.path.join(SRC, "deploy.env.example")

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


# Parameters only one template has, whose variable is nevertheless shared: {parameter: reason}
SHARED_VARIABLE_EXCEPTIONS = {
    "SytelineWsdl": "the plant takes the ERP address as one parameter, the stand-alone as an https/http pair; both read SYTELINE_WSDL",
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


BLOCK_MARKERS = (("shared", "# SHARED:"), ("plant", "# PLANT ONLY:"), ("stand-alone", "# STAND-ALONE ONLY:"))


def example_blocks(text):
    """{block: {VARIABLE: value}} of the single example file; the blocks start at their `# <NAME>:` header lines."""
    blocks, current = {name: {} for name, _ in BLOCK_MARKERS}, None
    for line in text.splitlines():
        for name, marker in BLOCK_MARKERS:
            if line.startswith(marker):
                current = name
        m = re.match(r"^#?\s*([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m and current:
            blocks[current][m.group(1)] = m.group(2)
    return blocks


def only_parameters(plant, standalone):
    """(parameters only the plant template has, parameters only the stand-alone template has)"""
    p, s = set(plant["Parameters"]), set(standalone["Parameters"])
    return sorted(p - s), sorted(s - p)


def example_violations(example, plant, standalone):
    """`example` is the text of deploy.env.example: shared settings once with the template default, mode-only ones in their block."""
    out, blocks = [], example_blocks(example)
    for name, _ in BLOCK_MARKERS:
        if not blocks[name]:
            out.append(f"example: the {name} block is missing or empty")
    for name in shared_parameters(plant, standalone):
        var = snake(name)
        default = plant["Parameters"][name].get("Default")
        if var not in blocks["shared"]:
            out.append(f"example: {name}: no {var}= line in the shared block")
        elif default not in (None, "") and blocks["shared"][var].strip().strip("\"'") != default:
            out.append(f"example: {name}: {var} is {blocks['shared'][var].strip()!r}, the template default is {default!r}")
    plant_only, standalone_only = only_parameters(plant, standalone)
    for names, home in ((plant_only, "plant"), (standalone_only, "stand-alone")):
        for name in names:
            if name in SHARED_VARIABLE_EXCEPTIONS:
                continue
            for block, variables in blocks.items():
                if snake(name) in variables and block != home:
                    out.append(f"example: {name}: {snake(name)} is in the {block} block, it belongs to the {home} block")
    seen = {}
    for block, variables in blocks.items():  # each variable once
        for var in variables:
            if var in seen:
                out.append(f"example: {var}: listed in the {seen[var]} block and the {block} block")
            seen[var] = block
    return out


def _code(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def cross_mode_violations(script, example):
    """Each deploy.sh mode must not read the other mode's variables (research.md R22)."""
    blocks = example_blocks(example)
    standalone_part, _ = _regions(script)
    start = standalone_part.index("# the plant network settings share the file")
    end = standalone_part.index("# ERP address:")
    standalone_code = _code(standalone_part[:start] + standalone_part[end:])  # the "ignored" notice names plant variables on purpose
    plant_code = _code(script.replace(standalone_part, ""))
    out = []
    for code, label, other in ((standalone_code, "stand-alone", "plant"), (plant_code, "plant", "stand-alone")):
        for var in sorted(blocks[other]):
            if re.search(rf"\b{var}\b", code):
                out.append(f"deploy.sh: the {label} branch reads {var}, which belongs to the {other} block")
    return out


# ---- the real files ---------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def files():
    read = lambda p: open(p).read()  # noqa: E731
    return {"plant": load_template(PLANT_TEMPLATE), "standalone": load_template(STANDALONE_TEMPLATE),
            "script": read(SCRIPT), "example": read(EXAMPLE)}


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


def test_the_example_file_lists_every_shared_setting_once_with_the_templates_default(files):
    no_violations(example_violations(files["example"], files["plant"], files["standalone"]))


def test_each_deploy_mode_reads_only_the_shared_settings_and_its_own_block(files):
    no_violations(cross_mode_violations(files["script"], files["example"]))


def test_no_separate_stand_alone_example_remains():
    assert not os.path.exists(os.path.join(SRC, "deploy.standalone.env.example"))


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


def test_a_shared_variable_missing_or_with_another_value_in_the_example_is_reported(files):
    example = re.sub(r"^SQL_LOGIN_TIMEOUT=.*$", "", files["example"], flags=re.M)
    found = example_violations(example, files["plant"], files["standalone"])
    assert found == ["example: SqlLoginTimeout: no SQL_LOGIN_TIMEOUT= line in the shared block"]
    example = re.sub(r"^SQL_ENCRYPT=true$", "SQL_ENCRYPT=false", files["example"], flags=re.M)
    found = example_violations(example, files["plant"], files["standalone"])
    assert len(found) == 1 and "SqlEncrypt" in found[0]


def test_a_variable_in_the_wrong_block_or_listed_twice_is_reported(files):
    example = files["example"].replace("ATTACH_VPC_ID=\n", "", 1)
    example = example.replace("SYTELINE_DEFAULT_WHSE=\n", "SYTELINE_DEFAULT_WHSE=\nATTACH_VPC_ID=\n", 1)  # a stand-alone variable in the plant block
    found = example_violations(example, files["plant"], files["standalone"])
    assert found == ["example: AttachVpcId: ATTACH_VPC_ID is in the plant block, it belongs to the stand-alone block"]
    twice = files["example"].replace("SYTELINE_DEFAULT_WHSE=\n", "SYTELINE_DEFAULT_WHSE=\nSQL_PORT=1433\n", 1)
    assert any("SQL_PORT: listed in the shared block and the plant block" in v
               for v in example_violations(twice, files["plant"], files["standalone"]))


def test_a_mode_reading_the_other_modes_variable_is_reported(files):
    script, (standalone_part, plant_part) = files["script"], _regions(files["script"])
    bad = script.replace(standalone_part, standalone_part.replace(
        'database_check="${DATABASE_CHECK:-auto}"', 'database_check="${DATABASE_CHECK:-auto}${SITE_ID:-}"'))
    assert cross_mode_violations(bad, files["example"]) == [
        "deploy.sh: the stand-alone branch reads SITE_ID, which belongs to the plant block"]
    bad = script.replace(plant_part, plant_part.replace('"$(q SiteId "${SITE_ID:-10}")"', '"$(q SiteId "${ATTACH_VPC_ID:-10}")"'))
    assert cross_mode_violations(bad, files["example"]) == [
        "deploy.sh: the plant branch reads ATTACH_VPC_ID, which belongs to the stand-alone block"]


def test_a_new_setting_added_to_both_templates_is_checked_automatically(files):
    plant, standalone = mutated(files)
    for t in (plant, standalone):
        t["Parameters"]["BrandNewSetting"] = {"Type": "String", "Default": "x"}
    assert "BrandNewSetting" in shared_parameters(plant, standalone)
    found = script_violations(files["script"], plant, standalone) + example_violations(
        files["example"], plant, standalone)
    assert any("BrandNewSetting" in v and "deploy.sh" in v for v in found)
    assert any("BrandNewSetting" in v and "BRAND_NEW_SETTING" in v for v in found)


def test_a_setting_that_exists_in_only_one_mode_is_never_an_error(files):
    plant, standalone = mutated(files)
    plant["Parameters"]["PlantOnlySetting"] = {"Type": "String", "Default": "x"}
    standalone["Parameters"]["StandaloneOnlySetting"] = {"Type": "String", "Default": "y"}
    assert template_violations(plant, standalone) == [] and wiring_violations(plant, standalone) == []
    assert script_violations(files["script"], plant, standalone) == []
    assert example_violations(files["example"], plant, standalone) == []
