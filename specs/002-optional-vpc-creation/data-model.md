# Data Model: Optional Host Network Creation

No stored data. This is the model of the deployment inputs and the resources they decide.

## Modes (derived from the four network identifiers)

| Identifiers supplied | Mode | Result |
|---|---|---|
| none of the four | **Create** | host network is created (needs `HostNetworkCidr` and `PlantCidr1`) |
| all four | **Supply** | nothing network-related is created, identical to feature 001 |
| some but not all | invalid | refused by `Rules` before anything is created, naming the missing ones |

## Inputs

| Input | Required when | Constraint |
|---|---|---|
| `WorkerVpcId`, `WorkerSubnetIds`, `WorkerSecurityGroupIds`, `WorkerRouteTableIds` | Supply (all four) | strings; the two lists are comma-separated; empty means "not supplied" |
| `HostNetworkCidr` | Create | IPv4 CIDR between /16 and /25 (pattern); no default; empty otherwise |
| `PlantCidr1` | Create | IPv4 CIDR; `PlantCidr2`, `PlantCidr3` optional |
| `DynamoDbPrefixListId` | optional | `pl-...`; tightens the DynamoDB egress rule |
| `CreateApiEndpoint` | optional | `false` (default) or `true`; true only with Create and no `ApiVpcEndpointId` |
| `ApiVpcEndpointId` | unless `CreateApiEndpoint=true` and Create | existing `execute-api` endpoint |
| `CreateVpcEndpoints` | Supply only (existing option) | ignored in Create mode (the endpoints are always created) |

## Resources created in Create mode (all conditioned on `CreateNetwork`)

| Resource | Notes |
|---|---|
| `HostVpc` | CIDR = `HostNetworkCidr`; DNS support and hostnames on |
| `HostSubnetA`, `HostSubnetB` | /26 each from `Fn::Cidr`, zones 0 and 1; no public IP mapping |
| `HostRouteTable` + two associations | local route only; no default route |
| `FunctionSecurityGroup` | no inbound |
| Egress rules | TCP 80 to `PlantCidr1`/`2`/`3` (2 and 3 conditional); TCP 443 to `HostNetworkCidr`; TCP 443 to the DynamoDB prefix list or `0.0.0.0/0` |
| `EndpointSecurityGroup` | inbound TCP 443 from `HostNetworkCidr` |
| DynamoDB gateway endpoint | attached to `HostRouteTable` |
| Secrets Manager interface endpoint | both subnets, private DNS |
| API-caller (`execute-api`) interface endpoint | only when `CreateApiEndpoint=true` |

Every optional resource above (`EgressPlant2`/`3`, the prefix-list egress rule, the API endpoint) carries a condition that includes `CreateNetwork`, so a stray Create-mode setting in Supply mode cannot create or reference anything.

Never created: internet gateway, NAT gateway, elastic IP, default route, VPN gateway, VPN connection, plant routes.

## Consumers (resolved by `CreateNetwork`)

| Consumer | Create mode | Supply mode |
|---|---|---|
| `WorkerFunction` and `DirectFunction` VPC settings | created subnets and `FunctionSecurityGroup` | supplied lists |
| DynamoDB / Secrets Manager endpoints | created network | supplied VPC, route tables, subnets, groups, when `CreateVpcEndpoints=true` |
| `OrderApi` endpoint list and resource-policy whitelist | created API endpoint if `CreateApiEndpoint=true`, otherwise `ApiVpcEndpointId` | `ApiVpcEndpointId` |

## Outputs (Create mode only)

`HostVpcId`, `HostSubnetIds`, `HostRouteTableId`, `FunctionSecurityGroupId`, `HostNetworkCidr`, `PlantCidrs`, `ApiEndpointId` (when created). The existing outputs (`ApiId`, function names, table name) are unchanged.
