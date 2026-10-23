# Deploying the direct-invoke function via the AWS Console (no SAM)

> **Superseded.** The deployment is now ONE function that does everything (API routes, the background work, the cleanup, the IDP event intake) and is deployed with SAM into an existing VPC: see `README.md` and `docs/DEPLOYMENT.md`. A console-built function with only the code below supports the direct actions at most and has no jobs table, no worker and no API. Keep this page only as a record of the manual steps.

> This covers only the direct-invoke function (`health`, `add_note`, `get_notes`). The private order API, worker, sweeper and job table are defined in `src/template.yaml` (deploy with SAM); see README.md. The function runs in the private subnets that route to the plant (outbound HTTP port 80 to the web service only; no database access) and keeps `SYTELINE_LIVE_WRITES=false` unless an approved test window.

## 1. What gets deployed

| Resource | Purpose |
|---|---|
| Secrets Manager secret `sytline/connect` | Holds `SYTELINE_PASS` (and optionally other keys) |
| IAM execution role `sytline-connect-role` | Logs + read the secret (+ VPC ENI access if in a VPC) |
| Lambda layer `sytline-connect-deps` | `zeep`, `lxml`, `requests` built for Linux |
| Lambda function `sytline-connect` | `src/lambda_function.py` + `src/syteline/`, handler `lambda_function.lambda_handler` |
| (Optional) VPC config | Only if SyteLine is private |

```
Test event ──► Lambda ──(VPC/NAT or public)──► SyteLine WSDL/SOAP
                 │
                 └─► Secrets Manager (password)      logs ─► CloudWatch
```

## 2. Key design decision: dependencies

`src/lambda_function.py` imports `zeep` (which pulls in `lxml`, a compiled library). The console code editor can't install packages, so pick one:

- **A. Layer (recommended, no code change).** Build the dependency zip on your Mac using pip's cross-platform flags — no Docker needed:
  ```
  mkdir -p build/python dist
  pip install -r src/requirements.txt -t build/python \
    --platform manylinux2014_x86_64 --implementation cp \
    --python-version 3.13 --only-binary=:all:
  (cd build && zip -r ../dist/deps-layer.zip python -x '*/__pycache__/*')
  (cd src && zip -r ../dist/function.zip lambda_function.py syteline handlers -x '*/__pycache__/*')
  ```
  Layer limit: 50 MB zipped for direct console upload, 250 MB unzipped total. If `deps-layer.zip` exceeds 50 MB, upload it to S3 and use "Upload a file from Amazon S3".
- **B. Stdlib-only rewrite.** Replace zeep with a hand-built SOAP envelope via `urllib`, then paste the code straight into the console editor. Fewer moving parts, but diverges from the proven `core.py` flow. Only worth it if layers are blocked.

The rest of this doc assumes **A**. Keep architecture and runtime consistent: **x86_64 + Python 3.13** everywhere (layer, function, pip flags).

## 3. Console steps

### 3.1 Secret
Secrets Manager → *Store a new secret* → *Other type of secret* → key/value `SYTELINE_PASS` = password → name `sytline/connect`. Note the ARN.

### 3.2 IAM role
IAM → Roles → *Create role* → AWS service → Lambda. Attach:
- `AWSLambdaBasicExecutionRole`
- `AWSLambdaVPCAccessExecutionRole` (only if using a VPC)
- Inline policy:
  ```json
  {"Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Action":"secretsmanager:GetSecretValue","Resource":"<secret ARN>"}]}
  ```
Name it `sytline-connect-role`.

### 3.3 Layer
Lambda → Layers → *Create layer* → name `sytline-connect-deps` → upload `dist/deps-layer.zip` → compatible runtime Python 3.13, architecture x86_64.

### 3.4 Function
Lambda → *Create function* → Author from scratch → name `sytline-connect`, Python 3.13, x86_64, existing role `sytline-connect-role`.
- Code → *Upload from → .zip file* → `dist/function.zip`.
- Layers → *Add a layer* → Custom layers → `sytline-connect-deps`.
- Configuration → General: Timeout **60 s**, Memory **256 MB**.
- Configuration → Environment variables:
  `SYTELINE_WSDL`, `SYTELINE_USER`, `SYTELINE_CONFIG` (e.g. `CCT`), `SYTELINE_SECRET_ARN`.
  Do **not** put the password here.

### 3.5 Networking (only if SyteLine is private)
Configuration → VPC → choose VPC, **private** subnets that route to SyteLine (VPN/Direct Connect/peering), and a security group allowing outbound to the SyteLine host:port.
If the function also needs Secrets Manager, give those subnets either a NAT gateway or a Secrets Manager VPC interface endpoint — otherwise `GetSecretValue` will time out. If SyteLine is public, leave the function **outside** any VPC.

### 3.6 Dry run (do this first)
Validates that the layer loads and the WSDL endpoint is reachable, **without credentials**. It only needs `SYTELINE_WSDL`, so it can be run before the secret, user and password exist (you can skip the secret in 3.1 and the user/secret env vars in 3.4 for this pass; the role still needs the basic execution policy, plus VPC access if applicable).

1. Lambda → `sytline-connect` → **Test** tab → *Create new event*, name `dry-run`.
2. Event JSON: `{"dry_run": true}` → *Save* → *Test*.
   (Alternative: set env var `SYTELINE_DRY_RUN=true` and test with `{}`.)
3. Expect `"ok": true` with stages `load`, `dns`, `tcp`, `wsdl`:
   - `load` — zeep/lxml/requests imported from the layer (proves the layer, runtime and architecture match).
   - `dns`, `tcp` — host resolves and the port is reachable from the function's network.
   - `wsdl` — a plain GET. **HTTP 401/403 is a pass** ("endpoint reachable, authentication required"); HTTP 2xx is parsed as WSDL; any other status fails.
4. If any stage fails, fix it using section 4 before moving on. No login is attempted and no secret is read in dry run.

### 3.7 Full test
Once the dry run passes and the secret/user are configured: Test tab → new event `{}` (with `SYTELINE_DRY_RUN` unset or `false`) → *Test*. This adds the `token` stage (`CreateSessionToken`), which proves the credentials.

## 4. Reading the result

| Failed stage | Likely cause |
|---|---|
| (before stages) `missing config` / secret error | env var typo, role lacks `GetSecretValue`, secret in another region |
| `load` (dry run) | layer not attached, wrong runtime/architecture (must be Python 3.13 / x86_64), or layer zip missing the `python/` folder |
| `dns` | wrong host, or private DNS not reachable from the VPC |
| `tcp` | security group / NACL / route / firewall allow-list (SyteLine may need to allow the NAT/VPN egress IP) |
| `wsdl` | HTTP 404/5xx = wrong URL path or service down (401/403 is *not* a failure in dry run); also TLS issue (try `SYTELINE_VERIFY_TLS=true/false`), proxy |
| `token` | bad user/password/config name, or user locked |

Logs: CloudWatch → `/aws/lambda/sytline-connect`.

## 5. Inputs needed before starting
WSDL URL · SyteLine user · config name · password · region · whether SyteLine is public or private (and if private: VPC, subnets, security group, egress path).

## 6. Security notes
- Password lives only in Secrets Manager; the function never logs or returns it.
- `SYTELINE_VERIFY_TLS` defaults to `false` (matches the original code); set `true` once the endpoint has a trusted cert.
- Scope the IAM secret permission to the single ARN.

## 7. Teardown
Delete the function, layer versions, role, secret (set a recovery window), and any VPC resources created for it.
