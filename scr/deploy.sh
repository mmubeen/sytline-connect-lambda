#!/usr/bin/env bash
# Build and deploy the SyteLine integration stack (us-gov-east-1, stack "sytline-connect").
#   ./deploy.sh            build + deploy (interactive change-set confirmation)
#   ./deploy.sh --build    build only
#   ./deploy.sh --validate lint the template only
# Settings come from deploy.env (copy deploy.env.example). Nothing secret is stored there.
set -euo pipefail
cd "$(dirname "$0")"

mode="deploy"
[[ "${1:-}" == "--build" ]] && mode="build"
[[ "${1:-}" == "--validate" ]] && mode="validate"

command -v sam >/dev/null || { echo "sam CLI not found (https://docs.aws.amazon.com/serverless-application-model/)"; exit 1; }

if [[ "$mode" == "validate" ]]; then
  sam validate --lint
  exit 0
fi

if [[ "$mode" == "build" ]]; then sam build; exit 0; fi

[[ -f deploy.env ]] || { echo "deploy.env missing: copy deploy.env.example and fill it in"; exit 1; }
set -a; . ./deploy.env; set +a

missing=()
for v in SYTELINE_WSDL SYTELINE_USER SYTELINE_SECRET_ARN API_VPCE_ID WORKER_VPC_ID WORKER_SUBNET_IDS \
         WORKER_SG_IDS WORKER_ROUTE_TABLE_IDS; do
  [[ -n "${!v:-}" ]] || missing+=("$v")
done
if (( ${#missing[@]} )); then echo "missing in deploy.env: ${missing[*]}"; exit 1; fi

live="${LIVE_WRITES:-false}"
if [[ "$live" == "true" ]]; then
  [[ -n "${LIVE_APPROVAL_REF:-}" ]] || { echo "LIVE_WRITES=true requires LIVE_APPROVAL_REF (the ERP owner's approval reference)"; exit 1; }
  echo "WARNING: deploying with LIVE ERP WRITES ENABLED (approval ref: $LIVE_APPROVAL_REF). Set LIVE_WRITES=false and redeploy afterwards."
else
  live="false"
fi

sam build

# Every value is double-quoted so commas (list parameters) and other characters are kept literally.
# (Verified with SAM's own parser: Key="a,b" -> a,b.)
q() { printf '%s="%s"' "$1" "$2"; }

sam deploy --parameter-overrides \
  "$(q SytelineWsdl "$SYTELINE_WSDL")" \
  "$(q SytelineUser "$SYTELINE_USER")" \
  "$(q SytelineSecretArn "$SYTELINE_SECRET_ARN")" \
  "$(q SytelineConfig "${SYTELINE_CONFIG:-CCT}")" \
  "$(q DefaultWhse "${SYTELINE_DEFAULT_WHSE:-}")" \
  "$(q DefaultOrdType "${SYTELINE_DEFAULT_ORDTYPE:-}")" \
  "$(q LiveWrites "$live")" \
  "$(q ApiVpcEndpointId "$API_VPCE_ID")" \
  "$(q WorkerVpcId "$WORKER_VPC_ID")" \
  "$(q WorkerSubnetIds "$WORKER_SUBNET_IDS")" \
  "$(q WorkerSecurityGroupIds "$WORKER_SG_IDS")" \
  "$(q WorkerRouteTableIds "$WORKER_ROUTE_TABLE_IDS")" \
  "$(q CreateVpcEndpoints "${CREATE_VPC_ENDPOINTS:-true}")"
