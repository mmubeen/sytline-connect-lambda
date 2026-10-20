# Research: Optional Host Network Creation

All decisions were prototyped against `cfn-lint`, `sam validate --lint` and `sam build` on a copy of the real template (see "Prototype results" at the end).

## R1 — How inputs become optional
- **Decision**: The four network inputs (`WorkerVpcId`, `WorkerSubnetIds`, `WorkerSecurityGroupIds`, `WorkerRouteTableIds`) become `String` parameters with `Default: ""` (the two ID lists stay comma-separated strings). A condition `CreateNetwork` is true when all four are empty. Consumers use `!If [CreateNetwork, <created>, <supplied>]`; list consumers use `!Split [",", !Ref ...]` for the supplied path.
- **Rationale**: `Rules` cannot call `Fn::Join`/`Fn::Select`, so emptiness of a list parameter cannot be asserted; plain strings can be compared with `Fn::Equals`. `AWS::EC2::VPC::Id` rejects an empty value, so `WorkerVpcId` becomes a plain `String`.
- **Alternatives**: nested stack (more files, no benefit); a macro (extra moving part).

## R2 — Refusing bad input before anything is created
- **Decision**: CloudFormation `Rules` are evaluated at create/update time before any resource is touched. Rules: (1) all-or-nothing network identifiers; (2) when creating, `HostNetworkCidr` and `PlantCidr1` are required; (3) the API-endpoint identifier is required unless the option creates one; (4) `CreateApiEndpoint=true` together with a supplied `ApiVpcEndpointId` is contradictory. `HostNetworkCidr` has an `AllowedPattern` (IPv4 CIDR, /16 to /25, or empty).
- **Rationale**: Meets FR-002, FR-004 and SC-003 with CloudFormation's own mechanism and clear messages. `deploy.sh` repeats the checks client-side for faster feedback.
- **Caveat**: the empty default makes cfn-lint warn (W1030); the template ignores that one check via its `Metadata` `cfn-lint` config, with a comment.
- **Alternatives**: a deliberately failing dummy resource (rolls back after partial creation, unclear errors) — rejected.

## R3 — Network layout (FR-003)
- **Decision**: One VPC (`HostNetworkCidr`, DNS support and hostnames on); two private subnets of size /26 cut from it with `Fn::Cidr [cidr, 2, 6]` in availability zones 0 and 1 of the region; one route table associated with both subnets and holding only the implicit local route. No internet gateway, NAT gateway, elastic IP or default route is defined anywhere.
- **Rationale**: Two zones suffice for tens of orders per hour; /26 (59 usable addresses) comfortably holds the function and endpoint network interfaces and works for any VPC from /16 to /25. `Fn::Select [1, Fn::GetAZs]` fails at changeset creation in a region with fewer than two zones, so the deployment is refused (spec edge case).
- **Alternatives**: three zones (unneeded); NAT for AWS access (spec says no internet needed and endpoints cover it).

## R4 — Private access to AWS services (FR-005)
- **Decision**: When the network is created, the DynamoDB gateway endpoint (attached to the created route table) and the Secrets Manager interface endpoint (both subnets, private DNS on, own security group allowing HTTPS from the network range) are always created. On a supplied network the existing `CreateVpcEndpoints` option keeps its meaning. The existing endpoint resources are reused with `VpcId`/route-table/subnet values chosen by `CreateNetwork`; their condition becomes `CreateNetwork` or `CreateVpcEndpoints=true`.
- **Rationale**: Feature 001 already requires these services; without them the worker cannot start in an isolated network.

## R5 — Security groups (FR-006)
- **Decision**: `FunctionSecurityGroup` (no inbound) with separate egress rules: TCP 80 to each plant range (`PlantCidr1`, optional `PlantCidr2`, `PlantCidr3`); TCP 443 to the host network range (interface endpoints); TCP 443 to the DynamoDB managed prefix list when `DynamoDbPrefixListId` is supplied, otherwise to `0.0.0.0/0`. Separate `AWS::EC2::SecurityGroupEgress` resources allow conditions per rule.
- **Rationale**: See Complexity Tracking in the plan: the managed prefix-list ID cannot be referenced from CloudFormation; with no internet route, the wide rule cannot leave the network.
- **Verification**: structural tests assert no default route exists and that the egress set matches the table above.

## R6 — Optional API-caller endpoint (FR-009)
- **Decision**: Parameter `CreateApiEndpoint` (`false`/`true`, default `false`). When true and the network is created, an `execute-api` interface endpoint is created in the two subnets with its own security group allowing HTTPS from the host network range; `OrderApi`'s `VPCEndpointIds` and `IntrinsicVpcWhitelist` use it instead of `ApiVpcEndpointId`. Callers outside the new network (or its peers) still need their own endpoint and a supplied ID.
- **Rationale**: matches the clarified "optional" answer without changing the default path.

## R7 — Handover outputs (FR-007, FR-010)
- **Decision**: Outputs present only when the network is created: `HostVpcId`, `HostSubnetIds`, `HostRouteTableId`, `FunctionSecurityGroupId`, `HostNetworkCidr`, `PlantCidrs`, and `ApiEndpointId` when created. The deployment guide gets a handover section: attach a gateway or transit attachment, add a route to the plant range in the route table, check plant DNS, then run the health check; it states the health check fails at `dns`/`tcp` until then and warns about address-range overlap.

## R8 — Switching between create and supply
- **Decision**: Not supported in place. `deploy.sh` reads the existing stack (`aws cloudformation describe-stacks`) and refuses a change of mode (presence of the `HostVpcId` output versus the new settings) unless `--allow-network-change` is given, printing what would be replaced.
- **Rationale**: CloudFormation would try to replace the network the functions run in. `Rules` cannot see previous state.

## R9 — Deletion
- **Decision**: Deleting the stack removes the created network. If the network team attached a gateway or added resources to the VPC, CloudFormation fails the VPC deletion safely (dependency violation); the guide says to detach them first. No `DeletionPolicy` is added (the network holds no data).

## R10 — Validation strategy and limits
- **Decision**: cfn-lint and `sam validate --lint` on the template; pytest loads the template with a CloudFormation-aware YAML loader to assert invariants (no internet gateway/NAT/EIP/default route, every network resource conditioned on `CreateNetwork`, outputs conditional, Lambda `VpcConfig` switches by condition); a small evaluator checks the `Rules` assertions for parameter combinations (all supplied, none supplied, each partial case, endpoint-option cases); `deploy.sh` is exercised with stub `sam`/`aws` commands.
- **Limits**: nothing is deployed from here, so availability-zone resolution, endpoint creation and IAM are verified by the linter only. The `Rules` evaluator supports the small function subset the template uses.

## Prototype results (copy of `scr/template.yaml` with the full network section, outside the repo)
- The optional-string parameters, four `Rules`, `Fn::Cidr`, `Fn::GetAZs`, conditional resources, `Fn::If` in the Lambda VPC settings, the API's `VPCEndpointIds` and the resource-policy whitelist all pass `cfn-lint` (with the one `W1030` check ignored) and `sam validate --lint`; `sam build` keeps the `Rules` and `Conditions` in the built template.
- **Bug found and designed out:** an optional egress rule that referenced the security group only checked its own setting (`PlantCidr2` non-empty). In Supply mode with a stray `PlantCidr2`, it would have referenced a group that does not exist. Rule: **every optional network resource's condition must include `CreateNetwork`** (`HasPlantCidr2`, `HasPlantCidr3`, `HasDdbPrefixList`, `MakeApiEndpoint`). A structural test asserts this for every resource that references a Create-mode resource.
- Create-mode-only settings (`HostNetworkCidr`, `PlantCidr*`, `DynamoDbPrefixListId`) given together with a full set of identifiers (Supply mode) are ignored by the template; `deploy.sh` warns about them.
- Not provable here: that `Fn::Select [1, Fn::GetAZs]` is refused in a one-zone region (the transform accepts it), and real endpoint/IAM creation.

## Open items (do not block planning)
1. Which address range the network team will give for the new network, and the plant ranges (inputs, not decisions).
2. Whether `DYNAMODB_PREFIX_LIST_ID` is available in GovCloud for the deployer (optional hardening only).
