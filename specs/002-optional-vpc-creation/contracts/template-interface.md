# Contract: template parameters, rules, outputs and deploy script

The "interface" of this feature is the SAM template (`scr/template.yaml`), its deployment script and the deployment outputs.

## New or changed parameters

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `WorkerVpcId` | String (was `AWS::EC2::VPC::Id`) | `""` | empty with the other three means Create |
| `WorkerSubnetIds` | String (was list) | `""` | comma-separated |
| `WorkerSecurityGroupIds` | String (was list) | `""` | comma-separated |
| `WorkerRouteTableIds` | String (was list) | `""` | comma-separated |
| `HostNetworkCidr` | String, pattern `^$\|^<IPv4>/(1[6-9]\|2[0-5])$` | `""` | required in Create mode |
| `PlantCidr1`, `PlantCidr2`, `PlantCidr3` | String, IPv4 CIDR pattern or empty | `""` | `PlantCidr1` required in Create mode |
| `DynamoDbPrefixListId` | String, `pl-` pattern or empty | `""` | optional hardening |
| `CreateApiEndpoint` | String `false`/`true` | `false` | |
| `ApiVpcEndpointId` | String (now optional) | `""` | required unless the option creates one |
| `CreateVpcEndpoints` | unchanged | `true` | Supply mode only |

Existing 001 deployments pass all four identifiers and keep working with no template parameter change in their `deploy.env` (the lists keep the same comma-separated format).

## Rules (refuse before creating anything)

| Rule | Condition | Message |
|---|---|---|
| `NetworkAllOrNothing` | any of the four supplied | "Supply all of WorkerVpcId, WorkerSubnetIds, WorkerSecurityGroupIds and WorkerRouteTableIds, or none to create the host network." |
| `CidrRequiredWhenCreating` | none of the four supplied | "HostNetworkCidr and PlantCidr1 are required when the host network is created." |
| `ApiEndpointRequired` | always | "Provide ApiVpcEndpointId, or set CreateApiEndpoint=true while creating the host network." |
| `ApiEndpointNotContradictory` | `CreateApiEndpoint=true` | "CreateApiEndpoint=true cannot be combined with an existing ApiVpcEndpointId." |

A region with fewer than two availability zones is refused at changeset creation (`Fn::Select` index out of range).

## Outputs (Create mode only)

`HostVpcId`, `HostSubnetIds`, `HostRouteTableId`, `FunctionSecurityGroupId`, `HostNetworkCidr`, `PlantCidrs`, `ApiEndpointId` (only when created). Network team's next steps: attach a gateway or transit attachment to `HostVpcId`, add a route to the plant range in `HostRouteTableId`, make plant DNS resolvable from the subnets.

## `deploy.sh` / `deploy.env`

New settings: `HOST_NETWORK_CIDR`, `PLANT_CIDR_1`, `PLANT_CIDR_2`, `PLANT_CIDR_3`, `DYNAMODB_PREFIX_LIST_ID`, `CREATE_API_ENDPOINT`. The four existing network settings and `API_VPCE_ID` become optional.

Client-side checks (same outcomes as the rules, before any AWS call):
- some but not all four network settings -> refuse, naming the missing ones;
- Create mode without `HOST_NETWORK_CIDR` or `PLANT_CIDR_1` -> refuse;
- `CREATE_API_ENDPOINT=true` with `API_VPCE_ID` set, or `API_VPCE_ID` empty without `CREATE_API_ENDPOINT=true` -> refuse;
- the existing stack was deployed in the other mode (read from its outputs) -> refuse unless `--allow-network-change`.
