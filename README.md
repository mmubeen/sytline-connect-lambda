# sytline-connect-lambda

Minimal Lambda that checks connectivity to the SyteLine SOAP endpoint, using the same
`CreateSessionToken` flow as `sytline/syteline/core.py::connect()` in the PO OCR project.

It reports four stages — `dns`, `tcp`, `wsdl`, `token` — stopping at the first failure.

## Config
| Variable | Notes |
|---|---|
| `SYTELINE_WSDL` | required |
| `SYTELINE_USER` | required |
| `SYTELINE_PASS` | required (or supply via secret) |
| `SYTELINE_CONFIG` | default `CCT` |
| `SYTELINE_SECRET_ARN` | optional Secrets Manager JSON, e.g. `{"SYTELINE_PASS": "..."}`; overrides env vars |
| `SYTELINE_VERIFY_TLS` | default `false` (matches original) |
| `SYTELINE_TIMEOUT` | default `10` s |
| `SYTELINE_DRY_RUN` | default `false`; or send event `{"dry_run": true}` |

## Dry run
Needs only `SYTELINE_WSDL`; no credentials or secret are read and no login is attempted. Stages: `load` (zeep/lxml/requests import = application load), `dns`, `tcp`, `wsdl`. The `wsdl` stage is a plain GET: **HTTP 401/403 passes** (reachable, auth required), 2xx is parsed as WSDL, anything else fails.

## Local run
    python -m venv .venv && . .venv/bin/activate && pip install -r scr/requirements.txt
    SYTELINE_WSDL=... SYTELINE_USER=... SYTELINE_PASS=... python scr/lambda_function.py

## Deploy (SAM)
`cd scr && sam build --use-container && sam deploy --guided`
(`--use-container` ensures `lxml` is built for Lambda's Linux runtime.)
If the endpoint is on a private network, uncomment `VpcConfig` in `scr/template.yaml`
(and make sure the subnets have a route to SyteLine).

## Deploy via console (no SAM)
See [`DEPLOY_PORTAL.md`](DEPLOY_PORTAL.md); prebuilt zips are in `dist/` (`deps-layer.zip`, `function.zip`).

## Invoke
`aws lambda invoke --function-name <name> out.json && cat out.json`
