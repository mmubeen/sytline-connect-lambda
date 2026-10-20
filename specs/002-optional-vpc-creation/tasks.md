---

description: "Task list for Optional Host Network Creation"
---

# Tasks: Optional Host Network Creation

**Input**: Design documents from `/specs/002-optional-vpc-creation/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/template-interface.md, quickstart.md

**Tests**: Included (plan.md specifies cfn-lint, structural pytest checks, a rules evaluator and `deploy.sh` tests).

**Organization**: Grouped by user story. Paths are relative to the repo root. This feature changes only `scr/template.yaml`, `scr/deploy.sh`, `scr/deploy.env.example`, docs and tests; no Lambda code changes. It extends feature 001's stack, so feature 001's safety rules still apply (live writes off, web service only).

**Hard rules**: nothing in these tasks defines an internet gateway, NAT gateway, elastic IP, default route, VPN gateway, VPN connection or plant route (FR-003, FR-008). **Every optional network resource's condition must include `CreateNetwork`** (research.md prototype finding). Nothing is deployed from this environment; tasks that need an AWS account are marked BLOCKED.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1 = deploy without an existing network (P1), US2 = deploy into an existing network (P2), US3 = hand over the new network (P3)

---

## Phase 1: Setup (test helpers)

- [X] T001 [P] Create `tests/cfn_template.py`: loads `scr/template.yaml` with a CloudFormation-aware YAML loader that keeps `!Ref`, `!Sub`, `!If`, `!Select`, `!Cidr`, `!GetAZs`, `!Split`, `!Join`, `!Equals`, `!Not`, `!And`, `!Or`, `!Condition`, `!GetAtt` as `{"Fn::X": ...}`/`{"Ref": ...}` dicts, plus a pytest fixture `template` (module-scoped)
- [X] T002 [P] Create `tests/cfn_eval.py`: a small evaluator over a parameter dict for the subset the template uses (`Ref` to a parameter, `Fn::Equals`, `Fn::Not`, `Fn::And`, `Fn::Or`, `Condition`) that (1) resolves the template's `Conditions` section, (2) evaluates each `Rules` rule (RuleCondition then Assertions) returning the list of violated `AssertDescription`s, (3) lists which resources are active (their `Condition` is true or absent); unsupported functions raise an error so a template change cannot silently pass

---

## Phase 2: Foundational (blocks all user stories)

- [X] T003 Update the `Parameters` of `scr/template.yaml` per contracts/template-interface.md: `WorkerVpcId` (was `AWS::EC2::VPC::Id`), `WorkerSubnetIds`, `WorkerSecurityGroupIds`, `WorkerRouteTableIds` become `String` with `Default: ""` (lists stay comma-separated); `ApiVpcEndpointId` gets `Default: ""`; add `HostNetworkCidr` (default "", `AllowedPattern` IPv4 CIDR /16 to /25 or empty), `PlantCidr1`, `PlantCidr2`, `PlantCidr3` (default "", IPv4 CIDR pattern or empty), `DynamoDbPrefixListId` (default "", `pl-` pattern or empty), `CreateApiEndpoint` (`false`/`true`, default `false`); add template `Metadata` `cfn-lint` config ignoring `W1030` with a comment explaining the empty defaults
- [X] T004 Add the `Conditions` to `scr/template.yaml`: `CreateNetwork` (all four network identifiers empty), `MakeApiEndpoint` (`CreateNetwork` and `CreateApiEndpoint` = `true`), `HasPlantCidr2`, `HasPlantCidr3` and `HasDdbPrefixList` (each = `CreateNetwork` AND its setting non-empty), and change `MakeEndpoints` to `CreateNetwork` OR `CreateVpcEndpoints` = `true`
- [X] T005 Add the four `Rules` to `scr/template.yaml` with the exact messages from contracts/template-interface.md: `NetworkAllOrNothing`, `CidrRequiredWhenCreating` (needs `HostNetworkCidr` and `PlantCidr1`), `ApiEndpointRequired` (`ApiVpcEndpointId` non-empty, or `CreateApiEndpoint` = `true` while creating), `ApiEndpointNotContradictory`
- [X] T006 [P] Rules tests `tests/unit/test_template_rules.py` using T001/T002: no identifiers + both CIDRs + `ApiVpcEndpointId` → accepted; none + `CreateApiEndpoint=true` and no `ApiVpcEndpointId` → accepted; all four + `ApiVpcEndpointId` → accepted; each of the four identifiers missing alone → refused with the `NetworkAllOrNothing` message; Create mode without `HostNetworkCidr` or without `PlantCidr1` → refused; no `ApiVpcEndpointId` and option off → refused; option on with `ApiVpcEndpointId` → refused; option on with a supplied network and no endpoint ID → refused; a few `HostNetworkCidr`/`PlantCidr` pattern cases (valid /16, /25; invalid /8, /26, text)

**Checkpoint**: parameters, conditions and rules in place; `cfn-lint` and `sam validate --lint` still pass for the template with its new defaults.

---

## Phase 3: User Story 1 - Deploy without an existing network (P1) 🎯 MVP

**Goal**: With none of the network identifiers supplied (and the address ranges given), the deployment creates the private host network and attaches the integration's functions to it.

**Independent Test**: `pytest tests/unit/test_template_network.py` (plus `./deploy.sh --validate`); in a real account, quickstart steps 4–5 (BLOCKED here).

### Tests

- [X] T007 [P] [US1] Structure tests `tests/unit/test_template_network.py` (Create-mode parameters, via T001/T002): the active resources include `HostVpc`, two subnets in availability zones 0 and 1 (`Fn::Select` 0 and 1 of `Fn::GetAZs`), one route table with two associations and **no `AWS::EC2::Route` resource**, `FunctionSecurityGroup` with no ingress rule, egress: TCP 80 to `PlantCidr1` (and `PlantCidr2`/`PlantCidr3` only when set), TCP 443 to the host range, TCP 443 to the prefix list when given else `0.0.0.0/0`; the DynamoDB gateway endpoint and the Secrets Manager interface endpoint use the created VPC, route table and subnets; `WorkerFunction` and `DirectFunction` `VpcConfig` use the created subnets and security group; subnet cidr bits are 6 (`/26`) and DNS support/hostnames are on
- [X] T008 [P] [US1] Invariant tests in the same file: **no resource anywhere in the template** has type `AWS::EC2::InternetGateway`, `AWS::EC2::NatGateway`, `AWS::EC2::EIP`, `AWS::EC2::VPNGateway`, `AWS::EC2::VPNConnection`, `AWS::EC2::TransitGateway*`, `AWS::EC2::VPCGatewayAttachment` or `AWS::EC2::CustomerGateway`; no route has destination `0.0.0.0/0`; subnets have `MapPublicIpOnLaunch: false`; **every resource that references a Create-mode resource (`HostVpc`, `HostSubnetA/B`, `HostRouteTable`, `FunctionSecurityGroup`, `ApiEndpoint`) has a `Condition` that includes `CreateNetwork`**, directly or through a condition built from it
- [X] T009 [P] [US1] API endpoint option tests in the same file: option off → no `ApiEndpoint` resource and `OrderApi` uses `ApiVpcEndpointId` for both `VPCEndpointIds` and the resource-policy whitelist; option on in Create mode → `ApiEndpoint` is active (interface endpoint, both subnets, endpoint security group allowing HTTPS from the host range) and both `OrderApi` settings use it; option on with a supplied network → no `ApiEndpoint` resource and the rules refuse the combination

### Implementation

- [X] T010 [US1] Add the network resources to `scr/template.yaml`, all with `Condition: CreateNetwork`: `HostVpc` (`CidrBlock: !Ref HostNetworkCidr`, DNS support and hostnames on), `HostSubnetA` and `HostSubnetB` (`!Select [0|1, !Cidr [!Ref HostNetworkCidr, 2, 6]]`, availability zone `!Select [0|1, !GetAZs ""]`, no public IPs), `HostRouteTable` and two `AWS::EC2::SubnetRouteTableAssociation`s; no gateway and no route (comment: the network team adds the plant route)
- [X] T011 [US1] Add the security groups to `scr/template.yaml`: `FunctionSecurityGroup` (no ingress; inline egress TCP 80 to `PlantCidr1`), separate `AWS::EC2::SecurityGroupEgress` resources for `PlantCidr2` (`HasPlantCidr2`), `PlantCidr3` (`HasPlantCidr3`), TCP 443 to `HostNetworkCidr`, TCP 443 to `DynamoDbPrefixListId` (`HasDdbPrefixList`) and TCP 443 to `0.0.0.0/0` (`CreateNetwork` and no prefix list; comment that no internet route exists and why); `EndpointSecurityGroup` with inbound TCP 443 from `HostNetworkCidr`
- [X] T012 [US1] Rewire the existing endpoints in `scr/template.yaml`: `DynamoDbEndpoint` and `SecretsManagerEndpoint` take `VpcId`, route tables, subnets and security groups from `!If [CreateNetwork, <created>, <supplied via !Split [",", ...]>]` (the Secrets Manager endpoint uses `EndpointSecurityGroup` in Create mode); condition `MakeEndpoints`
- [X] T013 [US1] Rewire the functions in `scr/template.yaml`: `WorkerFunction` and `DirectFunction` `VpcConfig.SubnetIds` and `SecurityGroupIds` use `!If [CreateNetwork, [created...], !Split [",", ...supplied]]`
- [X] T014 [US1] Add the optional API endpoint to `scr/template.yaml`: `ApiEndpoint` (`AWS::EC2::VPCEndpoint`, interface, `execute-api` service name via `!Sub`, both subnets, `EndpointSecurityGroup`, private DNS, `Condition: MakeApiEndpoint`); point `OrderApi`'s `VPCEndpointIds` and `Auth.ResourcePolicy.IntrinsicVpcWhitelist` at `!If [MakeApiEndpoint, !Ref ApiEndpoint, !Ref ApiVpcEndpointId]`
- [X] T015 [US1] Extend `scr/deploy.sh` and `scr/deploy.env.example` for Create mode: new settings `HOST_NETWORK_CIDR`, `PLANT_CIDR_1`, `PLANT_CIDR_2`, `PLANT_CIDR_3`, `DYNAMODB_PREFIX_LIST_ID`, `CREATE_API_ENDPOINT` passed as quoted parameter overrides; the four existing network settings and `API_VPCE_ID` become optional (only passed when set); client-side refusals identical to the rules (Create mode without `HOST_NETWORK_CIDR`/`PLANT_CIDR_1`; `API_VPCE_ID` empty without `CREATE_API_ENDPOINT=true`; `CREATE_API_ENDPOINT=true` with `API_VPCE_ID` set); an overlap reminder printed in Create mode
- [X] T016 [P] [US1] `deploy.sh` tests `tests/unit/test_deploy_script.py` (stub `sam`/`aws` on `PATH`, temp copy of `scr/` with a `deploy.env`): Create mode passes the new parameters and none of the four identifiers; each client-side refusal prints its message and does not call `sam deploy`; `CREATE_API_ENDPOINT=true` is passed through

**Checkpoint**: Create mode complete: `cfn-lint`, `sam validate --lint`, `sam build` and the new tests pass.

---

## Phase 4: User Story 2 - Deploy into an existing network (P2)

**Goal**: With all four identifiers supplied, nothing network-related is created and behavior is identical to feature 001; partial sets are refused before anything is created.

**Independent Test**: `pytest tests/unit/test_template_network.py tests/unit/test_deploy_script.py` (Supply-mode cases); quickstart step 6 in a real account (BLOCKED here).

### Tests

- [X] T017 [P] [US2] Supply-mode tests in `tests/unit/test_template_network.py`: with all four identifiers (a feature-001 style parameter set) the active resources contain none of `HostVpc`, `HostSubnetA/B`, `HostRouteTable`, the associations, `FunctionSecurityGroup`, any egress rule, `EndpointSecurityGroup`, `ApiEndpoint`; stray Create-mode settings (`HostNetworkCidr`, `PlantCidr1/2/3`, `DynamoDbPrefixListId`) alongside a full set activate nothing and reference nothing missing; with `CreateVpcEndpoints=true` the two endpoints are active against the supplied VPC and with `false` they are not; the `Fn::Split` of the supplied subnet and security-group strings resolves to the same lists as feature 001 (existing deployments see no change to the functions' network settings, SC-006)
- [X] T018 [P] [US2] `deploy.sh` tests in `tests/unit/test_deploy_script.py`: a feature-001 style `deploy.env` (all four identifiers, `API_VPCE_ID`, no new settings) deploys with the same parameters as before plus defaults; a partial set (each of the four missing in turn) is refused naming the missing ones; Create-mode settings alongside a full set produce a warning that they are ignored; the mode-switch guard (below) refuses when the stub `aws cloudformation describe-stacks` reports an existing stack deployed in the other mode, prints what would be replaced, and passes with `--allow-network-change`

### Implementation

- [X] T019 [US2] Extend `scr/deploy.sh`: refuse a partial set of the four network settings naming the missing ones; warn when Create-mode settings are given alongside a full set; add the mode-switch guard: when the stack already exists (`aws cloudformation describe-stacks`), determine its mode from the presence of the `HostVpcId` output and refuse a change of mode unless `--allow-network-change` is passed, printing that the network the functions run in would be replaced; keep every feature-001 behavior (including the live-writes approval check) unchanged

**Checkpoint**: Supply mode proven unchanged by tests; US1 and US2 both work.

---

## Phase 5: User Story 3 - Hand over the new network (P3)

**Goal**: After a Create-mode deployment the network team gets everything needed to connect it to the plant, and the guide says what remains.

**Independent Test**: outputs tests plus reading `docs/DEPLOYMENT.md`; quickstart step 5 in a real account (BLOCKED here).

- [X] T020 [P] [US3] Outputs tests in `tests/unit/test_template_network.py`: in Create mode the outputs `HostVpcId`, `HostSubnetIds`, `HostRouteTableId`, `FunctionSecurityGroupId`, `HostNetworkCidr`, `PlantCidrs` exist, and `ApiEndpointId` only when `MakeApiEndpoint`; in Supply mode none of them is emitted; the existing outputs (`ApiId`, function names, table name) are unchanged in both modes
- [X] T021 [US3] Add the conditional `Outputs` to `scr/template.yaml` (`Condition: CreateNetwork`; `ApiEndpointId` with `MakeApiEndpoint`; `HostSubnetIds` and `PlantCidrs` as comma-joined strings)
- [X] T022 [P] [US3] Update `docs/DEPLOYMENT.md`: a "Network: create or supply" section (the two modes, the new `deploy.env` settings, the table of what is created and never created); the handover steps for the network team (attach a gateway or transit attachment to `HostVpcId`, add a route to the plant range in `HostRouteTableId`, make plant DNS resolvable from the subnets, then run the health check, which is expected to fail at `dns`/`tcp` until then); the address-range overlap warning; the optional API-caller endpoint (who can use it); the no-in-place-mode-switch rule and `--allow-network-change`; the teardown caveat (a gateway attached by the network team must be detached before `sam delete`); update the prerequisites and parameter table so the network items are optional
- [X] T023 [P] [US3] Update `README.md` deploy section with a short pointer to the new create-or-supply behavior and `docs/DEPLOYMENT.md`; update the comments in `scr/deploy.env.example` so each network setting says "leave empty to create the host network"

**Checkpoint**: all three stories done.

---

## Phase 6: Polish & Cross-Cutting

- [X] T024 Full local validation: `cfn-lint scr/template.yaml`, `sam validate --lint --region us-gov-east-1` (from `scr/`), `sam build` (the build must still keep the `Rules` and `Conditions`), and the whole pytest suite (feature 001 tests must still pass); fix anything found
- [X] T025 [P] Cross-feature consistency check: feature 001's spec docs do not name the old required network settings, so no change was needed there (only `docs/DEPLOYMENT.md`, `README.md` and `scr/deploy.env.example`, updated in T022/T023)
- [ ] T026 BLOCKED (needs an AWS GovCloud account and the network team): run `specs/002-optional-vpc-creation/quickstart.md` steps 4–7 (Create-mode deployment, handover, Supply-mode update of an existing 001 stack with no network change, teardown) and record the results there; verify a region with fewer than two availability zones is refused at changeset creation if one can be tried

---

## Dependencies & Execution Order

- Phase 1 → Phase 2 → stories. T003 → T004 → T005 are sequential (same file, later items refer to earlier ones); T006 needs T001, T002 and T005.
- US1: tests T007–T009 need T001/T002 and are written before T010–T014; T010 → T011 → T012 → T013 → T014 are sequential (same file); T015 needs T003; T016 needs T015.
- US2: T017/T018 can be written once T010–T014 and T015 exist (they exercise both modes); T019 needs T015 (same file) and makes T018 pass.
- US3: T021 needs T010; T020 needs T021 for its pass; T022/T023 can start after T015 and T021.
- Polish: T024 last among code tasks; T026 last and BLOCKED.

### Parallel opportunities

- Setup: T001, T002. Foundational: T006 (after T005).
- US1 tests T007, T008, T009 together; T016 alongside T010–T014 once T015 is done.
- US2 tests T017, T018 together. US3: T020, T022, T023 together.
- Polish: T025.

## Implementation Strategy

1. **MVP**: Phases 1–3 (T001–T016): Create mode works and is proven by tests and the linter; Supply mode must not regress, so run the full suite after each template task.
2. Add US2 (guard and compatibility tests), then US3 (outputs and docs), then Polish.
3. T026 needs a real account, a network team and, for the connection steps, the plant; nothing before it requires them.
4. After each template change run `cfn-lint scr/template.yaml` and `sam validate --lint` (T024 repeats this at the end).
