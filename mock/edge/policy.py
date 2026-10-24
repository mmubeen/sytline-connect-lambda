"""Evaluate the function role's IAM statements against one request (specs/008 R4, FR-008).

Supports what the template uses: Allow and Deny, `Action` and `Resource` lists with `*` / `?` wildcards. Out of scope (and
listed as fidelity gaps): resource policies, key policies, permission boundaries, SCPs and condition keys.
"""

import fnmatch


def _match(patterns, value, fold):
    value = value.lower() if fold else value
    for p in patterns:
        p = p.lower() if fold else p
        if fnmatch.fnmatchcase(value, p):
            return True
    return False


def evaluate(statements, action, resource):
    """(allowed, reason). An explicit Deny wins; without an Allow the request is denied."""
    allowed = False
    for s in statements:
        if not (_match(s["Action"], action, True) and _match(s["Resource"], resource, False)):
            continue
        if s["Effect"] == "Deny":
            return False, "of an explicit deny in an identity-based policy"
        allowed = True
    if allowed:
        return True, "allowed"
    return False, f"no identity-based policy allows the {action} action"


def denial_message(role_arn, action, resource, reason):
    """The sentence AWS puts in an AccessDenied error."""
    return f"User: {role_arn} is not authorized to perform: {action} on resource: {resource} because {reason}"
