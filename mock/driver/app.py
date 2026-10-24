"""Schedule and API entry of the mock environment (specs/008 R8): turns an HTTP-style request into the API Gateway proxy
event the function receives in production, using the routes the deployment template declares."""

import json
import re


def _pattern(resource):
    return re.compile("^" + re.sub(r"\{([^}/]+)\}", r"(?P<\1>[^/]+)", resource) + "$")


def build_event(routes, method, path, body=None, headers=None, query=None):
    """The proxy event for `method path`; LookupError (a 404) when no declared route matches."""
    method = method.upper()
    for m, resource in routes:
        match = _pattern(resource).match(path)
        if m == method and match:
            return {
                "resource": resource, "path": path, "httpMethod": method,
                "headers": headers or {}, "queryStringParameters": query or None,
                "pathParameters": match.groupdict() or None,
                "body": body if isinstance(body, str) or body is None else json.dumps(body),
                "isBase64Encoded": False,
                "requestContext": {"resourcePath": resource, "httpMethod": method, "stage": "prod",
                                   "identity": {"userArn": "arn:aws-us-gov:iam::000000000000:user/mock-caller",
                                                "sourceIp": "127.0.0.1"}},
            }
    raise LookupError(f"no route {method} {path}")
