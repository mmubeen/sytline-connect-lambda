"""Render what the deployment template says the function is: environment, limits, routes, schedule and permissions.

The mock environment takes everything from src/template.yaml through this module (specs/008 R4, FR-008, FR-012), so it
cannot drift from the deployment without a test noticing. Any CloudFormation function it does not understand raises
`Unsupported` instead of being guessed.
"""

import os
import re
import sys

REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(REPO, "tests"))

from cfn_eval import Evaluator  # noqa: E402
from cfn_template import load_template  # noqa: E402

STACK = "sytline-connect"
ACCOUNT = "000000000000"
REGION = "us-gov-east-1"
PARTITION = "aws-us-gov"

def mock_params(account=ACCOUNT, region=REGION, partition=PARTITION):
    """Parameters the template requires; a developer's own values are never read from the host environment."""
    return {
        "SytelineWsdl": "http://erp:8080/IDORequestService/IDOWebService.asmx?wsdl",
        "SytelineUser": "mock-user",
        "SytelineSecretArn": f"arn:{partition}:secretsmanager:{region}:{account}:secret:mock/sytline-AbCdEf",
        "WorkerVpcId": "vpc-0a1b2c3d",
        "WorkerSubnetIds": "subnet-0a1b2c3d,subnet-0e4f5a6b",
        "WorkerSecurityGroupIds": "sg-0a1b2c3d",
        "ApiVpcEndpointId": "vpce-0a1b2c3d",
        "IdpStackName": "mock-idp",
        "IdpOutputBucketName": "mock-idp-output",
        "IdpTrackingTableName": "mock-idp-tracking",
        "IdpKmsKeyArns": f"arn:{partition}:kms:{region}:{account}:key/00000000-0000-4000-8000-000000000001",
        "IdpStateMachineArn": f"arn:{partition}:states:{region}:{account}:stateMachine:mock-idp-workflow",
        "DefaultWhse": "MAIN",
        "DefaultOrdType": "R",
    }


MOCK_PARAMS = mock_params()

# SAM policy templates the function uses, expanded the way SAM expands them
DYNAMO_CRUD = ["dynamodb:GetItem", "dynamodb:DeleteItem", "dynamodb:PutItem", "dynamodb:Scan", "dynamodb:Query",
               "dynamodb:UpdateItem", "dynamodb:BatchWriteItem", "dynamodb:BatchGetItem", "dynamodb:DescribeTable",
               "dynamodb:ConditionCheckItem"]


class Unsupported(Exception):
    pass


class _Resolver:
    def __init__(self, template, params, stack, account=ACCOUNT, region=REGION, partition=PARTITION):
        self.t = template
        self.ev = Evaluator(template, **params)
        self.stack = stack
        self.account, self.region, self.partition = account, region, partition
        self.pseudo = {"AWS::StackName": stack, "AWS::Region": region, "AWS::AccountId": account,
                       "AWS::Partition": partition}

    # physical names / attributes of the resources the function refers to
    def physical(self, name):
        return {"JobsTable": f"{self.stack}-JobsTable", "DirectFunction": f"{self.stack}-fn",
                "OrderApi": "mockapi0001"}.get(name, f"{self.stack}-{name}")

    def attr(self, name, attribute):
        if name == "DirectFunction" and attribute == "Arn":
            return f"arn:{self.partition}:lambda:{self.region}:{self.account}:function:{self.stack}-fn"
        if attribute == "Arn":
            return f"arn:{self.partition}:mock:{self.region}:{self.account}:{name}"
        raise Unsupported(f"GetAtt {name}.{attribute}")

    def ref(self, name):
        if name in self.pseudo:
            return self.pseudo[name]
        if name in self.ev.params:
            return self.ev.params[name]
        if name in self.t["Resources"]:
            return self.physical(name)
        raise Unsupported(f"Ref {name}")

    def sub(self, text, local):
        def one(m):
            key = m.group(1)
            if key in local:
                return str(local[key])
            if "." in key:
                return self.attr(*key.split(".", 1))
            return str(self.ref(key))

        return re.sub(r"\$\{([^}]+)\}", one, text)

    def resolve(self, e):
        if isinstance(e, list):
            return [x for x in (self.resolve(i) for i in e) if x is not _NOVALUE]
        if not isinstance(e, dict):
            return e
        (only,) = e if len(e) == 1 else (None,)
        if only is None or not (only.startswith("Fn::") or only in ("Ref", "Condition")):
            return {k: v for k, v in ((k, self.resolve(v)) for k, v in e.items()) if v is not _NOVALUE}
        (k, v), = e.items()
        if k == "Ref":
            return _NOVALUE if v == "AWS::NoValue" else self.ref(v)
        if k == "Fn::GetAtt":
            return self.attr(*v)
        if k == "Fn::If":
            return self.resolve(v[1] if self.ev.condition(v[0]) else v[2])
        if k == "Fn::Split":
            text = self.resolve(v[1])
            return text.split(v[0]) if text != "" else [""]
        if k == "Fn::Join":
            return v[0].join(str(x) for x in self.resolve(v[1]))
        if k == "Fn::Select":
            return self.resolve(v[1])[int(v[0])]
        if k == "Fn::Sub":
            if isinstance(v, str):
                return self.sub(v, {})
            return self.sub(v[0], {n: self.resolve(x) for n, x in v[1].items()})
        if k in ("Fn::Equals", "Fn::Not", "Fn::And", "Fn::Or", "Condition"):
            return self.ev.eval(e)
        raise Unsupported(k)


_NOVALUE = object()


def _statements(policies, r):
    partition, region, account = r.partition, r.region, r.account
    out = []
    for p in policies:
        if "Statement" in p:
            for s in p["Statement"]:
                out.append({"Effect": s["Effect"], "Action": _listed(s["Action"]), "Resource": _listed(s["Resource"])})
        elif "DynamoDBCrudPolicy" in p:
            table = f"arn:{partition}:dynamodb:{region}:{account}:table/{p['DynamoDBCrudPolicy']['TableName']}"
            out.append({"Effect": "Allow", "Action": list(DYNAMO_CRUD), "Resource": [table, table + "/index/*"]})
        else:
            raise Unsupported(f"policy {sorted(p)}")
    return out


def _listed(x):
    return list(x) if isinstance(x, list) else [x]


def render(params=None, template=None, stack=STACK, account=ACCOUNT, region=REGION, partition=PARTITION):
    """The function as the template deploys it, with mock parameter values (overridable)."""
    t = template or load_template()
    values = mock_params(account, region, partition)
    values.update(params or {})
    for name in values:
        if name not in t["Parameters"]:
            raise KeyError(f"unknown parameter {name}")
    r = _Resolver(t, values, stack, account, region, partition)
    fn = t["Resources"]["DirectFunction"]["Properties"]
    glob = t.get("Globals", {}).get("Function", {})
    env = {}
    for source in (glob.get("Environment", {}).get("Variables", {}), fn.get("Environment", {}).get("Variables", {})):
        for name, expr in source.items():
            env[name] = str(r.resolve(expr))
    statements = _statements(r.resolve(fn["Policies"]), r)
    routes = []
    if r.ev.condition("HasApi"):
        for path, methods in t["Resources"]["OrderApi"]["Properties"]["DefinitionBody"]["paths"].items():
            routes += [(m.upper(), path) for m in methods]
    sweep = fn["Events"]["Sweep"]["Properties"]
    import json

    return {
        "function_name": r.resolve(fn["FunctionName"]),
        "function_arn": r.attr("DirectFunction", "Arn"),
        "handler": fn["Handler"],
        "timeout": fn["Timeout"],
        "memory": fn.get("MemorySize", glob.get("MemorySize", 128)),
        "env": env,
        "routes": sorted(routes),
        "statements": statements,
        "schedule": {"expression": sweep["Schedule"], "input": json.loads(sweep["Input"])},
        "role_arn": f"arn:{partition}:sts::{account}:assumed-role/{stack}-DirectFunctionRole/{stack}-fn",
        "stack": stack,
        "parameters": values,
    }


if __name__ == "__main__":  # `python mock/render_env.py` prints the rendered settings as JSON (no secrets are involved)
    import json

    print(json.dumps(render(), indent=2, default=list))
