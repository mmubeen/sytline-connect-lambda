"""Real-IDP mode (specs/008 R14..R21): the function's IDP reads go to the developer's real IDP, everything else stays mocked.

  Credentials     the developer's temporary credentials, read from a private file on every use (never held in the environment)
  RealConfig      the real IDP identifiers: which requests are IDP-bound, and where the real endpoints are
  read_only       the allow-list of what may leave the machine
  RealForward     re-sign an allowed request with the developer's credentials and send it to the real service

Nothing here prints, logs or returns a credential value.
"""

import calendar
import logging
import os
import time

import requests

# botocore logs the canonical request, session token included, at DEBUG: that logger is never allowed below WARNING
logging.getLogger("botocore.auth").setLevel(logging.WARNING)

# the only actions that may reach the real side (FR-019)
READ_ONLY = frozenset({
    "cloudformation:DescribeStacks", "cloudformation:ListStackResources", "cloudformation:DescribeStackResource",
    "cloudformation:DescribeStackResources", "s3:GetObject", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:BatchGetItem",
    "lambda:InvokeFunction"})

_KEEP = {"content-type", "x-amz-target", "x-amz-invocation-type", "x-amz-log-type", "x-amz-client-context", "range",
         "if-match", "if-none-match", "if-modified-since", "if-unmodified-since"}
_DROP_BACK = {"content-length", "transfer-encoding", "content-encoding", "connection"}


class CredentialsUnavailable(Exception):
    """The credentials cannot be used; the message names why (never a value)."""


def _parse_expiry(text):
    try:
        return calendar.timegm(time.strptime(text.strip().replace("+00:00", "Z"), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


class Credentials:
    def __init__(self, path, clock=time.time):
        self._path, self._clock = path, clock

    def __repr__(self):
        return "Credentials(<hidden>)"

    __str__ = __repr__

    def _read(self):
        """(values, problem): problem is a short reason or None."""
        try:
            mode = os.stat(self._path).st_mode & 0o077
        except OSError:
            return None, "credentials missing"
        if mode:
            return None, "credentials file is readable by others (expected mode 0600)"
        values = {}
        with open(self._path) as f:
            for line in f.read().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
        if not all(values.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")):
            return None, "credentials file is incomplete"
        expiry = _parse_expiry(values.get("AWS_CREDENTIAL_EXPIRATION", ""))
        if expiry is not None and expiry <= self._clock():
            when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expiry))
            return None, f"credentials expired at {when}"
        return values, None

    def status(self):
        return self._read()[1] or "up"

    def load(self):
        values, problem = self._read()
        if problem:
            raise CredentialsUnavailable(problem)
        return values["AWS_ACCESS_KEY_ID"], values["AWS_SECRET_ACCESS_KEY"], values["AWS_SESSION_TOKEN"]


class RealConfig:
    def __init__(self, stack, bucket, table, region, partition, fips=None, endpoint_for=None):
        self.stack, self.bucket, self.table = stack, bucket, table
        self.region, self.partition = region, partition
        self.fips = (partition == "aws-us-gov") if fips is None else fips
        self._endpoint_for = endpoint_for

    def endpoint(self, service):
        if self._endpoint_for:
            return self._endpoint_for(service)
        return f"https://{service}{'-fips' if self.fips else ''}.{self.region}.amazonaws.com"

    def is_idp_bound(self, req, needs):
        """True when the request names the IDP stack, its output bucket or tracking table, or its lookup function."""
        for action, resource in needs:
            service = action.split(":", 1)[0]
            if service == "cloudformation" and f":stack/{self.stack}/" in resource:
                return True
            if service == "s3" and resource.split(":::", 1)[-1].split("/", 1)[0] == self.bucket:
                return True
            if service == "dynamodb" and resource.endswith(f":table/{self.table}"):
                return True
            if service == "lambda" and f":function:{self.stack}-LookupFunction-" in resource:
                return True
        return False


def read_only(needs):
    return bool(needs) and all(action in READ_ONLY for action, _ in needs)


class RealForward:
    def __init__(self, config, credentials, session=None):
        self.config, self.credentials = config, credentials
        self._http = session or requests

    def __call__(self, method, path, headers, query, body, service):
        from botocore.auth import S3SigV4Auth, SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials as BotoCredentials

        key, secret, token = self.credentials.load()   # raises CredentialsUnavailable
        url = self.config.endpoint(service) + path
        keep = {k: v for k, v in headers.items() if k.lower() in _KEEP and v}
        request = AWSRequest(method=method, url=url, data=body, headers=keep)
        signer = (S3SigV4Auth if service == "s3" else SigV4Auth)(BotoCredentials(key, secret, token), service, self.config.region)
        signer.add_auth(request)
        r = self._http.request(method, url, headers=dict(request.headers.items()), data=body, timeout=60, allow_redirects=False)
        out = {k: v for k, v in r.headers.items() if k.lower() not in _DROP_BACK}
        return r.status_code, out, r.content
