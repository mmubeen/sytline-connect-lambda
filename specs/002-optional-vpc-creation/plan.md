# Implementation Plan: Optional Host Network Creation

**Branch**: `002-optional-vpc-creation` | **Date**: 2026-10-20 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/002-optional-vpc-creation/spec.md`

## Summary

Make the host-network inputs of the existing SAM stack (feature 001, `src/template.yaml`) optional. If the deployer supplies none of them, the stack creates a private host network: one VPC with two private subnets (two availability zones), one route table with no route to the internet, security groups, a DynamoDB gateway endpoint and a Secrets Manager interface endpoint, and optionally the API-caller (`execute-api`) endpoint. If the deployer supplies all of them, nothing network-related is created and behavior is unchanged. Partial, missing or contradictory inputs are refused by CloudFormation `Rules` before anything is created. The plant connection (VPN, routes) is never created; the stack outputs what the network team needs to connect it. The approach is entirely template, deploy-script and documentation work; no Lambda code changes. See [research.md](research.md).

## Technical Context

**Language/Version**: AWS SAM / CloudFormation YAML (`src/template.yaml`); bash (`src/deploy.sh`); Python 3 for tests (existing pytest suite)

**Primary Dependencies**: SAM CLI, cfn-lint (already used); no new runtime dependency

**Storage**: N/A (infrastructure only; the job table from feature 001 is untouched)

**Testing**: cfn-lint + `sam validate --lint`; pytest structural tests that load the template; a small Python evaluator for the template's `Rules` over parameter combinations; `deploy.sh` tests with stubbed `sam`/`aws`; no real deployment from this environment

**Target Platform**: AWS GovCloud us-gov-east-1 (feature 001 decision)

**Project Type**: infrastructure-as-code change to an existing serverless stack

**Performance Goals**: N/A. A deployment that creates the network adds a few minutes (VPC endpoints) to the first deploy

**Constraints**: strictly additive (SC-006); no internet gateway, NAT, elastic IP or default route (FR-003/SC-004); VPC address range has no default (FR-004); no plant connection created (FR-008); live-write and web-service-only rules of feature 001 unchanged (FR-011); CloudFormation `Rules` cannot call `Fn::Join`/`Fn::Select`, so list-type inputs become comma-separated `String` parameters

**Scale/Scope**: one network, two subnets, two functions attached; tens of orders per hour

## Constitution Check

`.specify/memory/constitution.md` is still the unfilled template, so there are no ratified gates. The spec's own rules were applied (FR-001 to FR-011). **Result: PASS (no violations).** Post-design re-check: PASS. One refinement of FR-006 is recorded under Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/002-optional-vpc-creation/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── template-interface.md   # parameters, rules, outputs, deploy.sh behavior
└── tasks.md                    # created by /speckit-tasks
```

### Source Code (repository root)

```text
src/
├── template.yaml        # + optional-network parameters, Conditions, Rules, network resources, conditional outputs
├── deploy.sh            # + new settings, client-side checks, create/supply mode-switch guard
├── deploy.env.example   # + HOST_NETWORK_CIDR, PLANT_CIDR_1..3, CREATE_API_ENDPOINT, DYNAMODB_PREFIX_LIST_ID
docs/
└── DEPLOYMENT.md        # + "Network: create or supply", handover steps, teardown caveat
tests/unit/
├── test_template_network.py   # structure invariants (no IGW/NAT/EIP/default route, conditions, outputs)
├── test_template_rules.py     # evaluates the Rules for parameter combinations
└── test_deploy_script.py      # deploy.sh behavior with stubbed sam/aws
README.md                      # deploy section pointer
```

**Structure Decision**: Keep the network inside the existing stack and template as an optional section (spec Assumption), not a nested or separate stack: one `sam deploy`, one set of outputs, and the existing functions can reference the created resources directly. All new resources carry the single `CreateNetwork` condition so the supplied-network path is unchanged.

## Complexity Tracking

| Item | Why needed | Simpler alternative rejected because |
|---|---|---|
| Egress rule for HTTPS to `0.0.0.0/0` (when no DynamoDB prefix list is given) | Gateway endpoints reach DynamoDB through AWS-managed address ranges that CloudFormation cannot reference; the function security group must allow HTTPS to them | A rule limited to the network's own range would block DynamoDB. The route table has no internet route, so the rule cannot reach beyond the endpoint. An optional `DYNAMODB_PREFIX_LIST_ID` tightens it to exactly the managed prefix list, which satisfies FR-006 literally |
| Up to three plant address ranges as separate parameters | CloudFormation cannot create a variable number of security-group rules from one list | A single list would need a macro or custom resource |
