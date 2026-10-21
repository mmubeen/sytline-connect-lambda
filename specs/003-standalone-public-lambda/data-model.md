# Data Model: Stand-alone Public Lambda (No VPC)

No stored data (no job table). This is the model of the deployment inputs and the resources they decide.

## Inputs (template parameters)

| Parameter | Required | Constraint |
|---|---|---|
| `SytelineWsdlHttps` | exactly one of the two addresses | pattern `^$\|^https://[^ ]+$` |
| `SytelineWsdlHttp` | (see above) | pattern `^$\|^http://[^ ]+$`; only with `AllowPlainHttp=true` |
| `AllowPlainHttp` | no | `false` (default) or `true` |
| `SytelineUser` | yes | |
| `SytelineSecretArn` | yes | Secrets Manager secret with `SYTELINE_PASS` |
| `SytelineConfig` | no | default `CCT` |
| `LiveWrites` | no | `false` (default) or `true`; `true` only with an approval reference (enforced by `deploy.sh`) |
| `VerifyTls` | no | `true` (default) or `false` |
| `UseFipsEndpoints` | no | `true` (default) or `false` |

**Absent by design:** every network parameter of features 001/002 (`Worker*`, `HostNetworkCidr`, `PlantCidr*`, `DynamoDbPrefixListId`, `ApiVpcEndpointId`, `CreateApiEndpoint`, `CreateVpcEndpoints`).

## Resources (all unconditional)

| Resource | Notes |
|---|---|
| `DepsLayer` | layer `sytline-connect-standalone-deps`; same build as the VPC version |
| `DirectFunction` | handler `lambda_function.lambda_handler`; no VPC configuration, no events, no URL; environment: `SYTELINE_WSDL` (chosen by the address parameters), `SYTELINE_USER`, `SYTELINE_CONFIG`, `SYTELINE_SECRET_ARN`, `SYTELINE_LIVE_WRITES`, `SYTELINE_REQUIRE_HTTPS=true`, `SYTELINE_ALLOW_HTTP`, `SYTELINE_VERIFY_TLS`, `AWS_USE_FIPS_ENDPOINT` |
| Execution role (generated) | basic logging + read of the one secret |

Never present: any `AWS::EC2::*` resource, any API Gateway or function URL, any invoke permission for a public principal, the job table, the order API, the worker, the sweeper.

## Run-time guard

| Condition | Outcome |
|---|---|
| guard on (`SYTELINE_REQUIRE_HTTPS=true`), address `https://` | proceeds |
| guard on, address `http://`, override off | `ok:false`, `INSECURE_TRANSPORT`, nothing sent (dry run included) |
| guard on, address `http://`, override on | proceeds; `health` adds a warning that credentials cross the network unencrypted |
| guard off (VPC version) | unchanged |

## Outputs

`DirectFunctionName`, `DirectFunctionArn`, `PlainHttpOverride` (`true`/`false`, so the deployment output states the exposure), `LiveWrites`.
