# Research: Stand-alone Public Lambda (No VPC)

A prototype of the new template was validated before planning (`cfn-lint`, `sam validate --lint`, `sam build` with the existing `Makefile`, and the existing rules evaluator): it builds with no VPC configuration, the rules are kept in the built template, and the parameter and rule logic behaves as described in R3. Outside the repository.

## R1 — Packaging and independence (FR-001, FR-003, SC-005)
- **Decision**: A second SAM template, `src/template-standalone.yaml`, deployed as its own stack (`sytline-connect-standalone`) with its own layer (`sytline-connect-standalone-deps`). It shares `src/` as `CodeUri`/`ContentUri` and the existing `Makefile`.
- **Rationale**: Resource logical IDs `DepsLayer` and `DirectFunction` are the same in both templates, so the Makefile's `build-DepsLayer` and `build-DirectFunction` targets build both. A separate stack deploys, updates and is deleted without touching the VPC deployment.
- **Alternatives**: a conditional section inside the existing template (would still carry network parameters and rules and risk regressions in the VPC stack); a copied repository (duplicates code).

## R2 — No VPC, no public address (FR-002, FR-005, FR-006)
- **Decision**: One function (`DirectFunction`) with no `VpcConfig`, no `Events`, no function URL and no API; no `AWS::EC2::*` resource of any kind. Permissions: the execution role may read the one secret; there is no resource policy granting invoke to anyone, so only same-account principals that hold `lambda:InvokeFunction` through IAM can call it. The guide explains how to grant that.
- **Rationale**: Matches the clarified "cloud credentials only, no public web address". Lambda outside a VPC reaches Secrets Manager and the ERP through its default public egress, so no endpoints are needed.
- **Alternatives**: function URL with IAM auth (rejected by the clarification); public API Gateway (rejected).

## R3 — HTTPS by default at deployment time (FR-007)
- **Decision**: Parameters `SytelineWsdlHttps` (pattern `^$|^https://[^ ]+$`), `SytelineWsdlHttp` (pattern `^$|^http://[^ ]+$`) and `AllowPlainHttp` (`false`/`true`, default `false`). Rules: exactly one of the two addresses is given; if the `http://` one is given, `AllowPlainHttp` must be `true`. The function's `SYTELINE_WSDL` is chosen with `Fn::If`. `deploy.sh` maps the single `SYTELINE_WSDL` setting to the right parameter by its scheme and sets `AllowPlainHttp` from `ALLOW_PLAIN_HTTP`.
- **Rationale**: CloudFormation rules cannot test a string prefix, so the scheme is carried by which parameter is used; the patterns make a wrong scheme in the wrong parameter an error. The refusal happens before anything is created.
- **Prototype**: https accepted; http without the override refused with the rule message; http with the override accepted; none or both refused; `http://` in the https parameter refused by its pattern.

## R4 — HTTPS at run time (FR-007, SC-007)
- **Decision**: New environment variables read by the application: `SYTELINE_REQUIRE_HTTPS` (set to `true` only by the stand-alone template; absent in the VPC deployment, so the plant's HTTP keeps working), `SYTELINE_ALLOW_HTTP` (the override). A helper in `syteline/config.py` returns a problem message when the guard is on, the ERP address is `http://` and the override is off. It is applied (a) in `health.run_health` before any network call, including the dry run (so nothing is sent to the ERP), (b) in `lambda_function` before the notes actions build a client, and (c) inside `SoapIdoClient` before the SOAP client is created. Result: `ok: false`, error code `INSECURE_TRANSPORT`, message saying what to change. With the override on, `health` adds a `warnings` entry that credentials cross the network unencrypted; the stage list is unchanged.
- **Rationale**: Covers the "address changed after deployment" edge case and defense in depth. The VPC version is unaffected because the guard is off there.
- **Alternatives**: guard always on (would break the plant's HTTP deployments).

## R5 — TLS verification
- **Decision**: The stand-alone template sets `SYTELINE_VERIFY_TLS` from a parameter `VerifyTls` that defaults to `true` (the application's own default stays `false` for compatibility with the original plant behavior). `deploy.sh` exposes `VERIFY_TLS` in the settings file.
- **Rationale**: Over the internet, an unverified certificate defeats HTTPS. The deployer can turn it off deliberately for a self-signed test ERP.

## R6 — Deployment tooling (FR-009)
- **Decision**: `deploy.sh --standalone` uses `sam ... --config-env standalone` (a `[standalone.*]` block in `samconfig.toml` naming the stack `sytline-connect-standalone`, the template `template-standalone.yaml` and the region), reads its own settings file `deploy.standalone.env`, keeps the `--profile`, `--validate`, `--build` switches and the live-writes approval check, refuses any network setting (`WORKER_*`, `HOST_NETWORK_CIDR`, `PLANT_CIDR_*`, `DYNAMODB_PREFIX_LIST_ID`, `API_VPCE_ID`, `CREATE_API_ENDPOINT`, `CREATE_VPC_ENDPOINTS`) as not applicable, refuses an `http://` address without `ALLOW_PLAIN_HTTP=true`, and prints a warning when the override is on. The create/supply mode-switch guard does not apply.
- **Rationale**: Reuses the profile handling and checks already tested; the separate settings file makes "network settings do not apply" a meaningful check.

## R7 — Safety rules carried over (FR-008, FR-011)
- **Decision**: The template sets `SYTELINE_LIVE_WRITES` from a `LiveWrites` parameter defaulting to `false`; `deploy.sh --standalone` requires `LIVE_APPROVAL_REF` to set it `true`. Credentials come only from the secret (no password parameter or environment variable). The existing structured logging and no-secrets tests already cover the handler; new tests add the stand-alone template.

## R8 — Documentation (FR-010, SC-006)
- **Decision**: `docs/STANDALONE.md`: when this version suits (ERP reachable from the internet, test/staging or vendor-hosted ERP, or published through a secure gateway) and when it does not (ERP only on the plant network: use the VPC version); a comparison table; the fixed-outbound-address caveat (no allow-listing); the HTTPS requirement and the override and its consequence; how to grant invoke permission to a principal; deploy, verify and remove steps.

## Open items (do not block planning)
1. Whether the SyteLine web service is offered over HTTPS at all (the instruction sheet says the plant service is HTTP today). Until it is, deployments use the override.
2. Nothing is deployed from this environment: Lambda egress, secret access and IAM behavior are verified by the linter and tests only.

## Attached mode (revision 2026-10-21)

Prototyped (`cfn-lint`, `sam validate`, `sam build`, and the SAM transform): the optional `VpcConfig` survives as an `Fn::If`.

### R9 — Optional attachment (FR-002, FR-012)
- **Decision**: Parameters `AttachVpcId`, `AttachSubnetIds`, `AttachSecurityGroupIds` (strings, default empty, identifier patterns). Condition `AttachToVpc` = all three non-empty; rule `AttachmentAllOrNothing` refuses a partial set. `VpcConfig: !If [AttachToVpc, {SubnetIds: !Split, SecurityGroupIds: !Split}, !Ref AWS::NoValue]`. The VPC id is not needed by Lambda itself; it is required so the deployer states the whole network and appears in the `AttachedVpcId` output (the stack cannot verify that the subnets belong to it).
- **Rationale**: Same pattern as feature 002 (all or none). One template, one stack name, no new resources, no API gateway or URL.

### R10 — Least-privilege role
- **Decision**: An explicit `DirectFunctionRole` (basic logging, one inline policy to read the secret, and the VPC network-interface managed policy **only when attached**) referenced by `Role:`; the function has no SAM `Policies`.
- **Rationale**: Prototype of the SAM transform showed SAM adds `AWSLambdaVPCAccessExecutionRole` whenever a `VpcConfig` exists, even a conditional one, and a duplicate explicit policy fails lint (E3037).

### R11 — Reaching the secret store when attached (FR-005, FR-013)
- **Decision**: The deployer provides the private path (existing Secrets Manager endpoint with private DNS, or a NAT route); the stack creates none. The secret read uses short timeouts (connect 3 s, read 5 s, one attempt) and any failure becomes one structured `SECRET_STORE_UNREACHABLE` error naming the store; with `diagnose`, the existing regional-endpoint probe shows whether the store's address resolves and connects, and a finding explains the fix.
- **Rationale**: A function in a VPC loses default internet access; without this, the secret read could hang for the SDK's default (minutes) and the check would time out with no explanation.
- **Alternatives**: the stack creating an endpoint (rejected by the clarification; conflicts with an existing endpoint's private DNS).
