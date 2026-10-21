# Contract: stand-alone deployment interface

## Template `scr/template-standalone.yaml`

Parameters and rules: see [data-model.md](../data-model.md). Rules (refused before anything is created):

| Rule | Message |
|---|---|
| `ExactlyOneWsdl` | "Give exactly one of SytelineWsdlHttps or SytelineWsdlHttp." |
| `PlainHttpNeedsOverride` | "An http:// ERP address needs AllowPlainHttp=true (credentials would cross the network unencrypted)." |

Outputs: `DirectFunctionName`, `DirectFunctionArn`, `PlainHttpOverride`, `LiveWrites`.

## Invocation

Direct Lambda invoke only (no URL, no API), by principals with `lambda:InvokeFunction` on the function. Actions are the same as feature 001's direct-invoke contract (`specs/001-syteline-erp-integration/contracts/lambda-actions.md`): `health` (with `dry_run`), `add_note`, `get_notes`. Differences:

- New error code `INSECURE_TRANSPORT`: `{"ok": false, "action": "...", "errors": [{"code": "INSECURE_TRANSPORT", "message": "..."}]}` returned by every action (the dry run included) before any ERP access, when the ERP address is `http://` and the override is off.
- `health` result gains an optional `warnings` array (only when the plain-HTTP override is on): `["plain HTTP override is on: credentials cross the network unencrypted"]`. The stage list is unchanged (SC-003).
- `create_order`/the order API do not exist in this deployment; `UNKNOWN_ACTION` is returned for any other action, as before.

## `deploy.sh --standalone`

`./deploy.sh --standalone [--validate|--build] [--profile NAME]` uses `--config-env standalone` (stack `sytline-connect-standalone`, template `template-standalone.yaml`) and the settings file `deploy.standalone.env`:

`SYTELINE_WSDL`, `SYTELINE_USER`, `SYTELINE_SECRET_ARN`, `SYTELINE_CONFIG`, `ALLOW_PLAIN_HTTP`, `VERIFY_TLS`, `LIVE_WRITES`, `LIVE_APPROVAL_REF`, `AWS_PROFILE_NAME`.

Refusals (before any AWS call): a network setting present (`WORKER_*`, `HOST_NETWORK_CIDR`, `PLANT_CIDR_*`, `DYNAMODB_PREFIX_LIST_ID`, `API_VPCE_ID`, `CREATE_API_ENDPOINT`, `CREATE_VPC_ENDPOINTS`) "not applicable to the stand-alone version"; an `http://` `SYTELINE_WSDL` without `ALLOW_PLAIN_HTTP=true`; `LIVE_WRITES=true` without `LIVE_APPROVAL_REF`; missing required settings. With the override on it prints that credentials will cross the network unencrypted. The existing `--profile` handling is unchanged.
