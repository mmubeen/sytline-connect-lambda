"""The mock-AWS edge (specs/008 R3): every AWS call of the function passes through here.

For each request it (1) identifies the IAM action, (2) checks it against the function role's statements from the template,
(3) applies the active faults, (4) records the call and (5) forwards it to Moto, answers it itself (Lambda Invoke) or
refuses it with an AWS-worded error. `/__mock__/...` is the control API (control.py).
"""

import json
import time
from xml.sax.saxutils import escape

from werkzeug.wrappers import Request, Response

from . import actions, control, models, policy, seed

JSON_SERVICES = ("dynamodb", "secretsmanager", "kms")


class Edge:
    def __init__(self, rendered, forward, runtime, backend):
        self.r = rendered
        self.forward = forward      # callable(method, path, headers, query, body) -> (status, headers, body_bytes)
        self.runtime = runtime      # invoke_sync(event, timeout) / invoke_async(event) of the function under test
        self.backend = backend      # the seeded world: health, reset, scenarios, key lookups, the IDP lookup function
        self.calls = models.CallLog()
        self.faults = models.FaultSet()
        self.partition, self.region, self.account = _scope(rendered)

    # -- WSGI ------------------------------------------------------------------------------
    def __call__(self, environ, start_response):
        request = Request(environ)
        if request.path.startswith("/__mock__"):
            response = control.handle(self, request)
        else:
            response = self.aws(request)
        return response(environ, start_response)

    # -- AWS calls -------------------------------------------------------------------------
    def aws(self, request):
        body = request.get_data()
        headers = dict(request.headers)
        req = actions.parse_request(request.method, request.path, headers, request.args.to_dict(), body)
        if not req["service"]:
            return Response("unsigned request", 403)
        needs = actions.identify_all(req, self.partition, self.region, self.account)
        action, resource = needs[0]
        record = dict(service="aws", operation=f"{req['service']}:{req['operation']}", request=req["params"],
                      caller=self.r["role_arn"], action=action, resource=resource)

        fault_id, reason = None, None
        for action, resource in needs:  # a transaction needs every one of its items' actions
            fault_id, reason = self._denial(req, action, resource)
            if reason:
                break
        if reason:
            self.calls.add(allowed=False, fault=fault_id, status=_status(req["service"], 403), **record)
            return self._deny(req["service"], action, resource, reason)

        if req["service"] == "secretsmanager" and self.faults.take("secrets.unavailable"):
            self.calls.add(allowed=True, fault="secrets.unavailable", status=503, **record)
            return _error(req["service"], 503, "ServiceUnavailable", "the service is temporarily unavailable")

        kms = self._kms(req, resource)
        if kms:
            key, fault_id, why = kms
            self.calls.add(allowed=False, fault=fault_id, status=403, **record)
            return self._deny(req["service"], "kms:Decrypt", key, why)

        if req["service"] == "lambda" and req["operation"] == "Invoke":
            return self._invoke(request, req, body, record)

        if req["service"] == "secretsmanager" and str(req["params"].get("SecretId", "")).startswith("arn:"):
            # Moto does not know this partition: look the secret up by name, which is what the ARN ends with
            body = json.dumps({**req["params"], "SecretId": seed.secret_name(req["params"]["SecretId"])}).encode()
        status, out_headers, out = self.forward(request.method, request.full_path.rstrip("?"), headers,
                                                request.args.to_dict(), body)
        self.calls.add(allowed=True, status=status, **record)
        return Response(out, status, out_headers)

    def _denial(self, req, action, resource):
        """(fault id or None, reason) when the request must be refused before it is served."""
        if action.startswith("lambda:") and self.faults.peek("lambda.invoke_denied") and \
                resource.endswith(":function:" + self.r["function_name"]):
            self.faults.take("lambda.invoke_denied")
            return "lambda.invoke_denied", "no identity-based policy allows the lambda:InvokeFunction action"
        if action.startswith("lambda:") and "-LookupFunction-" in resource and self.faults.take("idp.lookup_denied"):
            return "idp.lookup_denied", "no identity-based policy allows the lambda:InvokeFunction action"
        allowed, why = policy.evaluate(self.r["statements"], action, resource)
        return (None, None) if allowed else (None, why)

    def _kms(self, req, resource):
        """(key, fault id, reason) when reading an encrypted resource needs a key the role may not use."""
        if req["operation"] not in ("GetObject", "GetItem", "Query", "BatchGetItem"):
            return None
        fault = self.faults.peek("idp.kms_denied")
        key = (fault or {}).get("params", {}).get("key_arn") or self.backend.kms_key_for(resource)
        if not key:
            return None
        if fault:
            self.faults.take("idp.kms_denied")
            return key, "idp.kms_denied", "no identity-based policy allows the kms:Decrypt action"
        allowed, why = policy.evaluate(self.r["statements"], "kms:Decrypt", key)
        return None if allowed else (key, None, why)

    def _deny(self, service, action, resource, reason):
        message = policy.denial_message(self.r["role_arn"], action, resource, reason)
        return _error(service, 403, "AccessDeniedException" if service != "s3" else "AccessDenied", message)

    # -- Lambda Invoke ---------------------------------------------------------------------
    def _invoke(self, request, req, body, record):
        name = req["path"].split("/functions/", 1)[1].split("/invocations")[0].rsplit(":", 1)[-1]
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return _error("lambda", 400, "InvalidRequestContentException", "could not parse the request body")
        if name == self.r["function_name"]:
            if request.headers.get("X-Amz-Invocation-Type") == "Event":
                self.runtime.invoke_async(payload)
                self.calls.add(allowed=True, status=202, **record)
                return Response(b"", 202)
            result = self.runtime.invoke_sync(payload, None)
            self.calls.add(allowed=True, status=result["status"], **record)
            out = Response(json.dumps(result["payload"]), result["status"], {"Content-Type": "application/json"})
            if result.get("function_error"):
                out.headers["X-Amz-Function-Error"] = result["function_error"]
            return out
        if "-LookupFunction-" in name:
            self.calls.add(allowed=True, status=200, **record)
            return Response(json.dumps(self.backend.lookup(payload)), 200, {"Content-Type": "application/json"})
        self.calls.add(allowed=True, status=404, **record)
        return _error("lambda", 404, "ResourceNotFoundException", f"Function not found: {name}")


def _scope(rendered):
    parts = rendered["function_arn"].split(":")
    return parts[1], parts[3], parts[4]


def _status(service, default):
    return default


def _error(service, status, code, message):
    """An error body in the protocol of the service, as botocore expects it."""
    if service in JSON_SERVICES:
        status = 400 if status == 403 else status
        return Response(json.dumps({"__type": code, "message": message}), status,
                        {"Content-Type": "application/x-amz-json-1.1", "x-amzn-ErrorType": code})
    if service == "lambda":
        return Response(json.dumps({"Message": message}), status,
                        {"Content-Type": "application/json", "x-amzn-ErrorType": code})
    if service == "cloudformation":
        xml = (f"<ErrorResponse><Error><Type>Sender</Type><Code>{code}</Code><Message>{escape(message)}</Message>"
               "</Error></ErrorResponse>")
        return Response(xml, status, {"Content-Type": "text/xml"})
    xml = f"<Error><Code>{code}</Code><Message>{escape(message)}</Message></Error>"
    return Response(xml, status, {"Content-Type": "application/xml"})


def wait_until(predicate, timeout, interval=0.5):
    """Poll `predicate` until it is true or `timeout` seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
