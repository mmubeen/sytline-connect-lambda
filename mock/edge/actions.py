"""Read an AWS API request apart and map it to the IAM action and resource it needs (specs/008 R3, R4).

The service comes from the SigV4 credential scope, the operation from `X-Amz-Target` (JSON protocols), the `Action`
parameter (query protocol) or the method and path (S3, Lambda). Anything not understood is `unmapped:` and is denied by the edge.
"""

import json
import re
from urllib.parse import parse_qsl, unquote

STACK_ID = "00000000-0000-0000-0000-000000000000"


def _service(headers):
    m = re.search(r"Credential=[^/]+/\d+/[^/]+/([^/]+)/aws4_request", headers.get("Authorization", ""))
    return m.group(1) if m else None


def _body(body):
    if not body:
        return {}
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return dict(parse_qsl(text))


def parse_request(method, path, headers, query, body):
    """{service, operation, params} for one request; service is None when the request is not signed."""
    headers = {k.title(): v for k, v in headers.items()}
    service = _service(headers)
    params = _body(body)
    operation = None
    target = headers.get("X-Amz-Target")
    if target:
        operation = target.rsplit(".", 1)[-1]
    elif "Action" in params:
        operation = params["Action"]
    elif "Action" in (query or {}):
        operation = query["Action"]
        params = {**query, **params}
    elif service == "s3":
        operation = {"GET": "GetObject", "HEAD": "GetObject", "PUT": "PutObject", "DELETE": "DeleteObject"}.get(method)
    elif service == "lambda" and path.rstrip("/").endswith("/invocations"):
        operation = "Invoke"
    return {"service": service, "operation": operation, "params": params, "method": method, "path": path}


def identify_all(req, partition, region, account):
    """Every (IAM action, resource ARN) a request needs. A DynamoDB transaction or batch has no IAM action of its own: it
    needs the action of each item it carries (PutItem, UpdateItem, DeleteItem, ConditionCheckItem, GetItem)."""
    if req["service"] == "dynamodb" and req["operation"] in _DDB_MULTI:
        table = lambda name: f"arn:{partition}:dynamodb:{region}:{account}:table/{name}"  # noqa: E731
        p, found = req["params"], []
        if req["operation"] == "TransactWriteItems":
            for item in p.get("TransactItems", []):
                for kind, action in (("Put", "PutItem"), ("Update", "UpdateItem"), ("Delete", "DeleteItem"),
                                     ("ConditionCheck", "ConditionCheckItem")):
                    if kind in item:
                        found.append((f"dynamodb:{action}", table(item[kind].get("TableName", "*"))))
        elif req["operation"] == "BatchGetItem":
            found = [("dynamodb:BatchGetItem", table(name)) for name in p.get("RequestItems", {})]
        elif req["operation"] == "TransactGetItems":
            found = [("dynamodb:GetItem", table(i["Get"].get("TableName", "*"))) for i in p.get("TransactItems", []) if "Get" in i]
        else:  # BatchWriteItem
            for name, requests in p.get("RequestItems", {}).items():
                for r in requests:
                    found.append(("dynamodb:PutItem" if "PutRequest" in r else "dynamodb:DeleteItem", table(name)))
        return list(dict.fromkeys(found)) or [(f"dynamodb:{req['operation']}", "*")]
    return [identify(req, partition, region, account)]


_DDB_MULTI = ("TransactWriteItems", "TransactGetItems", "BatchWriteItem", "BatchGetItem")


def identify(req, partition, region, account):
    """(IAM action, resource ARN) for a parsed request."""
    service, op, params, path = req["service"], req["operation"], req["params"], req["path"]
    if not service or not op:
        return f"unmapped:{service}:{op}", "*"
    if service == "dynamodb":
        table = params.get("TableName")
        return f"dynamodb:{op}", (f"arn:{partition}:dynamodb:{region}:{account}:table/{table}" if table else "*")
    if service == "secretsmanager":
        return f"secretsmanager:{op}", params.get("SecretId", "*")
    if service == "s3":
        bucket, _, key = unquote(path.lstrip("/")).partition("/")
        return f"s3:{op}", f"arn:{partition}:s3:::{bucket}/{key}" if key else f"arn:{partition}:s3:::{bucket}"
    if service == "kms":
        return f"kms:{op}", params.get("KeyId", "*")
    if service == "cloudformation":
        name = params.get("StackName")
        return f"cloudformation:{op}", (f"arn:{partition}:cloudformation:{region}:{account}:stack/{name}/{STACK_ID}"
                                        if name else "*")
    if service == "lambda" and op == "Invoke":
        name = unquote(path.split("/functions/", 1)[1].split("/invocations")[0])
        return "lambda:InvokeFunction", (name if name.startswith("arn:") else
                                         f"arn:{partition}:lambda:{region}:{account}:function:{name}")
    return f"unmapped:{service}:{op}", "*"
