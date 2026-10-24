# Contract: real-IDP mode

## Start
`make -C mock up MOCK_IDP=real MOCK_IDP_PROFILE=<profile> MOCK_IDP_STACK=<stack> MOCK_IDP_BUCKET=<bucket> MOCK_IDP_TABLE=<table> MOCK_IDP_DOCUMENT=<id> [MOCK_IDP_KMS_KEYS=<arn,arn>] [MOCK_IDP_REGION=us-gov-east-1] [MOCK_IDP_FIPS=true|false]`

| Setting | Required | Meaning |
|---|---|---|
| `MOCK_IDP` | no (default `mock`) | `real` turns the mode on |
| `MOCK_IDP_PROFILE` | yes | profile whose temporary credentials are exported |
| `MOCK_IDP_STACK`, `MOCK_IDP_BUCKET`, `MOCK_IDP_TABLE` | yes | the same three values as the template's `IdpStackName`, `IdpOutputBucketName`, `IdpTrackingTableName` |
| `MOCK_IDP_DOCUMENT` | yes | a finished document id in that IDP |
| `MOCK_IDP_KMS_KEYS` | no | the template's `IdpKmsKeyArns` |

Refusals (exit before anything starts, each names the setting or the cause): a required setting is missing; the profile cannot export credentials;
`MOCK_IDP=real` together with a mock scenario selection; credentials already expired.

`make -C mock idp-creds` re-exports the credentials without restarting.

## Control API additions
| Route | Change |
|---|---|
| `GET /health` | adds `"idp": "up"` (mock mode: `"mock"`), or the reason it is not up; `ready` is false until it is `up`/`mock` |
| `GET /scenarios` | real mode lists only `real-idp`; mock mode lists the mock scenarios |
| `POST /scenario/real-idp` | resets, then returns the event built for `MOCK_IDP_DOCUMENT`; mock scenario names answer 409 with the reason |
| `GET /calls` | records carry `target: mock|real` |

## What may leave the machine (real mode)
Only: `cloudformation:DescribeStacks`, `ListStackResources`, `DescribeStackResource`, `DescribeStackResources` on the IDP stack; `s3:GetObject` on the
output bucket; `dynamodb:GetItem`, `Query`, `BatchGetItem` on the tracking table; `lambda:InvokeFunction` on `<stack>-LookupFunction-*`. Everything else
IDP-bound is answered `AccessDenied` by the edge and recorded `target: real, allowed: false`. No other real service is ever called.

## Errors
Missing/expired credentials: AWS-worded `ExpiredToken` (HTTP 400 for JSON services, 403 for S3) from the edge, recorded; health shows the reason.
