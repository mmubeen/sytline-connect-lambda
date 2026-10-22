# Implementation Plan: Stand-alone Public Lambda (No VPC)

**Branch**: `003-standalone-public-lambda` | **Date**: 2026-10-20 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/003-standalone-public-lambda/spec.md`

## Summary

**Revision 2026-10-21:** the stand-alone version can now optionally be **attached to an existing VPC, subnets and security group** (all three, or none; clarified in spec.md). Still direct-invoke only, no API gateway/URL/public address, and no network resources created. See "Attached mode" in research.md (R9–R11).

Add a second, independent SAM deployment of the direct-invoke Lambda (connectivity check including the dry run, `add_note`, `get_notes`) that by default runs **outside any VPC** with Lambda's default public networking. It is a separate template (`src/template-standalone.yaml`) and stack, reuses the existing application code and the same `Makefile` build, has **no network parameters or resources, no public web address and no function URL or API** (callable only with IAM-authorized cloud credentials), and refuses to talk to an `http://` ERP address unless the deployer explicitly switches on a plain-HTTP override. The HTTPS rule is enforced twice: at deployment (CloudFormation parameter patterns and rules) and at run time (a small guard in the application, switched on only in this deployment, so the VPC version keeps working over the plant's HTTP). `deploy.sh` gains a `--standalone` mode with its own settings file. See [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.13 (Lambda; existing code), SAM/CloudFormation YAML, bash

**Primary Dependencies**: zeep, lxml, requests, urllib3, boto3 (existing); no new dependency

**Storage**: none (no job table; this version excludes order upload)

**Testing**: pytest (existing suite) + structural tests of the new template via the existing `tests/cfn_template.py`/`tests/cfn_eval.py`; unit tests of the transport guard; `deploy.sh` tests with stubbed `sam`/`aws`; cfn-lint, `sam validate --lint`, `sam build`; no real deployment from this environment

**Target Platform**: AWS GovCloud us-gov-east-1; Lambda outside any VPC

**Project Type**: infrastructure and small application change to an existing serverless project

**Performance Goals**: N/A (same function, same behavior as the VPC version)

**Constraints**: no network input or resource in no-VPC mode, only the three attachment settings in attached mode, and the stack never creates network resources (FR-001/002/012); independent stack, no name collisions (FR-003); direct-invoke operations only (FR-004); no public address, IAM-only callers (FR-006); HTTPS required unless the override is on (FR-007); 001's safety rules unchanged (FR-008); the VPC version's behavior unchanged (SC-005)

**Scale/Scope**: one function, one layer, one stack; tens of calls per hour

## Constitution Check

`.specify/memory/constitution.md` is still the unfilled template, so there are no ratified gates. The spec's own rules were applied (FR-001 to FR-011). **Result: PASS (no violations).** Post-design re-check: PASS.

## Project Structure

### Documentation (this feature)

```text
specs/003-standalone-public-lambda/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── standalone-interface.md   # template parameters, rules, outputs, invocation, error code, deploy.sh mode
└── tasks.md                      # created by /speckit-tasks
```

### Source Code (repository root)

```text
src/
├── template-standalone.yaml        # NEW: layer + direct function, no VPC, no network parameters, no public address
├── samconfig.toml                  # + [standalone.*] config environment (stack, template, region)
├── deploy.sh                       # + --standalone mode (own settings file, HTTPS/override handling, rejects network settings)
├── deploy.standalone.env.example   # NEW settings example (git-ignored real file)
├── lambda_function.py              # direct-invoke handler: apply the transport guard before any ERP access
└── syteline/
    ├── config.py                   # + require_https(), allow_http(), transport_problem(url)
    ├── errors.py                   # + INSECURE_TRANSPORT, InsecureTransport
    ├── health.py                   # refuse an insecure address before any network call; warnings when the override is on
    └── client.py                   # defense in depth: refuse an insecure address before creating the SOAP client
docs/
└── STANDALONE.md                   # NEW: when to use it, exposure trade-offs, HTTPS/override, invoker permissions, verification, teardown
tests/unit/
├── test_template_standalone.py     # NEW
├── test_transport.py               # NEW
└── test_secret_store.py            # NEW: bounded, structured failure when Secrets Manager is unreachable (FR-013)
└── test_deploy_script.py           # + standalone cases
README.md, docs/DEPLOYMENT.md       # pointers
```

**Structure Decision**: A second template file next to the first, sharing `src/` as the code root and the existing `Makefile` (the logical IDs `DepsLayer` and `DirectFunction` are the same in both templates, so `make` targets are reused). Separate stack and layer names avoid collisions. The HTTPS rule needs run-time code, so it is a small guard driven by environment variables that only this template sets.

## Complexity Tracking

| Item | Why needed | Simpler alternative rejected because |
|---|---|---|
| Two ERP-address parameters (`SytelineWsdlHttps`, `SytelineWsdlHttp`) instead of one | CloudFormation can validate a parameter's own pattern but cannot test a prefix of one parameter against another | One `SytelineWsdl` parameter would make the "http:// needs the override" rule unenforceable at deployment time; the run-time guard alone would still protect, but the deployment would succeed and fail later |
| An explicit IAM role instead of SAM `Policies` | SAM adds the VPC network-interface permission whenever a `VpcConfig` exists, even a conditional one, so no-VPC deployments would carry permissions they do not use | Accepting SAM's default would break least privilege in no-VPC mode |
| A run-time guard in shared application code | The spec requires refusal "at deployment and at run time" (FR-007), and the address can change after deployment | Deployment-time checks only would leave the functions willing to send credentials over HTTP |
