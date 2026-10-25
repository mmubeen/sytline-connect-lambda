# Contract: the deployment

## Resources created (SC-001)
Exactly one `AWS::Serverless::Function` (`<stack>-fn`, timeout 300 s, 256 MB, python3.13, in the supplied subnets and security groups, no reserved concurrency, async retries 0), its role, the dependency layer, `JobsTable` (retained), `OrderApi` with its five routes, the `rate(5 minutes)` schedule, and, only when `IdpStateMachineArn` is given, the document event rule and permission. **No** `AWS::EC2::*` resource, no VPC endpoint, no security-group rule.

## Template parameters
| Parameter | Required | Meaning |
|---|---|---|
| `WorkerVpcId` | yes | existing VPC (`^vpc-[0-9a-f]+$`) |
| `WorkerSubnetIds` | yes | existing subnets, comma-separated (`^subnet-…(,subnet-…)*$`); one is allowed (warned by `deploy.sh`) |
| `WorkerSecurityGroupIds` | yes | existing security groups, comma-separated |
| `ApiVpcEndpointId` | **no** (default empty = no API) | existing `execute-api` interface endpoint (`^$|^vpce-[0-9a-f]+$`) the private API is reachable through |
| `SytelineWsdl`, `SytelineUser`, `SytelineSecretArn`, `SytelineConfig`, `DefaultWhse`, `DefaultOrdType`, `LiveWrites`, `UseFipsEndpoints` | as today | unchanged |
| route, SQL, tuning and IDP parameters, `IdpStateMachineArn` | as today | unchanged |
| removed | | `WorkerRouteTableIds`, `HostNetworkCidr`, `PlantCidr1..3`, `DynamoDbPrefixListId`, `CreateVpcEndpoints`, `CreateApiEndpoint`; also `SytelineWsdlHttps/Http`, `AllowPlainHttp`, `VerifyTls`, `Attach*`, `DatabaseCheck` |

## Outputs
`DirectFunctionName`, `FunctionArn`, `ApiId`, `JobsTableName`, `AttachedVpcId`. Removed: the network handover outputs (`HostVpcId`, `HostSubnetIds`, …).

## `deploy.env` (one block, one file)
Required: `SYTELINE_WSDL`, `SYTELINE_USER`, `SYTELINE_SECRET_ARN`, `WORKER_VPC_ID`, `WORKER_SUBNET_IDS`, `WORKER_SG_IDS`. Optional: `API_VPCE_ID` (empty = no API). Optional as today: `AWS_PROFILE_NAME`, `SYTELINE_CONFIG`, `SYTELINE_DEFAULT_*`, `LIVE_WRITES` / `LIVE_APPROVAL_REF`, `USE_FIPS_ENDPOINTS`, the database login block and options, route and tuning settings, IDP settings, `IDP_STATE_MACHINE_ARN`. Removed settings found in the file: a notice names them; they are not passed.

## `deploy.sh`
`./deploy.sh [--build|--validate|--profile NAME|--yes]`; no `--allow-network-change` and no option for an alternate deployment. It refuses (before building) a missing or malformed network identifier, warns on a single subnet, merges the database login into the secret as before, and prints a notice when the existing stack still has the old functions (see docs/MIGRATION.md).
