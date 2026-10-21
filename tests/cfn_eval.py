"""A small CloudFormation evaluator for template tests (T002).

Supports exactly what scr/template.yaml uses: Ref to a parameter, Fn::Equals/Not/And/Or, Condition,
Fn::If/Select/Split/Join/Cidr/GetAZs/Sub/GetAtt. Anything else raises, so a template change using an
unsupported function cannot silently pass the tests.
"""

import ipaddress
import re

AZS = ["az-0", "az-1", "az-2"]
NOVALUE = object()  # AWS::NoValue: dropped from lists and joins


class Unsupported(Exception):
    pass


class Evaluator:
    def __init__(self, template, **params):
        self.t = template
        self.params = {k: v.get("Default", "") for k, v in template.get("Parameters", {}).items()}
        for k, v in params.items():
            assert k in self.params, f"unknown parameter {k}"
            self.params[k] = v
        self._cond = {}

    # -- conditions / rules ------------------------------------------------------------
    def condition(self, name):
        if name not in self._cond:
            self._cond[name] = self.eval(self.t["Conditions"][name])
        return self._cond[name]

    def eval(self, e):
        """Evaluate an expression used in Conditions / Rules."""
        if not isinstance(e, dict):
            return e
        (k, v), = e.items()
        if k == "Ref":
            if v in self.params:
                return self.params[v]
            raise Unsupported(f"Ref to non-parameter {v} in a condition/rule")
        if k == "Condition":
            return self.condition(v)
        if k == "Fn::Equals":
            return self.eval(v[0]) == self.eval(v[1])
        if k == "Fn::Not":
            return not self.eval(v[0])
        if k == "Fn::And":
            return all(self.eval(x) for x in v)
        if k == "Fn::Or":
            return any(self.eval(x) for x in v)
        raise Unsupported(k)

    def violations(self):
        """AssertDescriptions of the template Rules that these parameters violate."""
        out = []
        for name, rule in (self.t.get("Rules") or {}).items():
            if "RuleCondition" in rule and not self.eval(rule["RuleCondition"]):
                continue
            for a in rule["Assertions"]:
                if not self.eval(a["Assert"]):
                    out.append(a["AssertDescription"])
        return out

    def parameter_errors(self):
        out = []
        for name, spec in self.t.get("Parameters", {}).items():
            val = self.params[name]
            if "AllowedPattern" in spec and not re.fullmatch(spec["AllowedPattern"], str(val)):
                out.append(f"{name}: does not match AllowedPattern")
            if "AllowedValues" in spec and val not in spec["AllowedValues"]:
                out.append(f"{name}: not in AllowedValues")
        return out

    def active(self):
        """Names of the resources that would be created."""
        return {n for n, r in self.t["Resources"].items()
                if "Condition" not in r or self.condition(r["Condition"])}

    def live_refs(self, expr):
        """Ref/GetAtt targets that are actually used: only the taken branch of each Fn::If."""
        if isinstance(expr, list):
            out = set()
            for x in expr:
                out |= self.live_refs(x)
            return out
        if not isinstance(expr, dict):
            return set()
        if len(expr) != 1:  # a plain mapping of properties
            out = set()
            for val in expr.values():
                out |= self.live_refs(val)
            return out
        (k, v), = expr.items()
        if k == "Fn::If":
            return self.live_refs(v[1] if self.condition(v[0]) else v[2])
        if k == "Ref":
            return {v}
        if k == "Fn::GetAtt":
            return {v[0]}
        if k == "Fn::Sub":
            return refs_in(expr)
        out = set()
        for val in expr.values():
            out |= self.live_refs(val)
        return out

    # -- property resolution -----------------------------------------------------------
    def resolve(self, e):
        """Resolve a resource property expression. Refs to resources become '@Name'."""
        if isinstance(e, list):
            return [x for x in (self.resolve(i) for i in e) if x is not NOVALUE]
        if not isinstance(e, dict):
            return e
        if len(e) != 1:  # a plain mapping of properties
            return {k: v for k, v in ((k, self.resolve(v)) for k, v in e.items()) if v is not NOVALUE}
        (k, v), = e.items()
        if k == "Ref":
            if v == "AWS::NoValue":
                return NOVALUE
            return self.params[v] if v in self.params else f"@{v}"
        if k == "Fn::GetAtt":
            return "@" + ".".join(v)
        if k == "Fn::If":
            return self.resolve(v[1] if self.condition(v[0]) else v[2])
        if k == "Fn::Select":
            return self.resolve(v[1])[int(v[0])]
        if k == "Fn::Split":
            return str(self.resolve(v[1])).split(v[0])
        if k == "Fn::Join":
            return v[0].join(str(x) for x in self.resolve(v[1]))
        if k == "Fn::GetAZs":
            return list(AZS)
        if k == "Fn::Sub":
            return v if isinstance(v, str) else v[0]
        if k == "Fn::Cidr":
            block, count, bits = self.resolve(v[0]), int(v[1]), int(v[2])
            net = ipaddress.ip_network(block)
            subs = list(net.subnets(new_prefix=32 - bits))
            return [str(s) for s in subs[:count]]
        if k in ("Fn::Equals", "Fn::Not", "Fn::And", "Fn::Or", "Condition"):
            return self.eval(e)
        raise Unsupported(k)


def refs_in(expr):
    """All Ref / GetAtt targets inside an expression."""
    out = set()
    if isinstance(expr, list):
        for x in expr:
            out |= refs_in(x)
    elif isinstance(expr, dict):
        for k, v in expr.items():
            if k == "Ref":
                out.add(v)
            elif k == "Fn::GetAtt":
                out.add(v[0])
            elif k == "Fn::Sub":
                text = v if isinstance(v, str) else v[0]
                out |= set(re.findall(r"\$\{([A-Za-z0-9]+)(?:\.[A-Za-z]+)?\}", text))
            else:
                out |= refs_in(v)
    return out
