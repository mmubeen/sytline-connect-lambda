#!/usr/bin/env bash
# Build and deploy the SyteLine integration stack (us-gov-east-1, stack "sytline-connect").
#   ./deploy.sh                          build + deploy (interactive change-set confirmation)
#   ./deploy.sh --build                  build only
#   ./deploy.sh --validate               lint the template only
#   ./deploy.sh --allow-network-change   permit switching between "create" and "supply" host-network mode
#   ./deploy.sh --profile NAME           use this AWS credentials profile (flag > AWS_PROFILE_NAME in deploy.env > default chain)
# Flags can be combined, e.g. ./deploy.sh --profile govcloud-prod --validate
# Settings come from deploy.env (copy deploy.env.example). Nothing secret is stored there.
set -euo pipefail
cd "$(dirname "$0")"

mode="deploy"
allow_network_change="false"
profile=""
while (( $# )); do
  case "$1" in
    --build) mode="build" ;;
    --validate) mode="validate" ;;
    --allow-network-change) allow_network_change="true" ;;
    --profile) [[ $# -ge 2 && -n "${2:-}" && "${2:0:2}" != "--" ]] || { echo "ERROR: --profile needs a profile name" >&2; exit 2; }
               profile="$2"; shift ;;
    --profile=*) profile="${1#--profile=}"; [[ -n "$profile" ]] || { echo "ERROR: --profile needs a profile name" >&2; exit 2; } ;;
    *) echo "unknown argument: $1"; exit 2 ;;
  esac
  shift
done

# deploy.env is optional for --build/--validate; read it early only for AWS_PROFILE_NAME
if [[ -z "$profile" && -f deploy.env ]]; then
  profile="$(set -a; . ./deploy.env; printf '%s' "${AWS_PROFILE_NAME:-}")"
fi

# --- AWS credentials profile: one switch, applied to every AWS-facing step ---------------
profile_args=()
if [[ -n "$profile" ]]; then
  [[ "$profile" =~ ^[A-Za-z0-9_.@-]+$ ]] || { echo "ERROR: invalid profile name: $profile" >&2; exit 2; }
  if command -v aws >/dev/null; then
    if profiles="$(aws configure list-profiles 2>/dev/null)"; then
      printf '%s\n' "$profiles" | grep -Fxq -- "$profile" \
        || { echo "ERROR: AWS profile '$profile' is not configured on this machine (aws configure list-profiles)" >&2; exit 1; }
    else
      echo "WARNING: could not list AWS profiles (older aws CLI?); not checking that '$profile' exists."
    fi
  fi
  profile_args=(--profile "$profile")
  echo "Using AWS profile: $profile"
fi

command -v sam >/dev/null || { echo "sam CLI not found (https://docs.aws.amazon.com/serverless-application-model/)"; exit 1; }

if [[ "$mode" == "validate" ]]; then
  sam validate --lint ${profile_args[@]+"${profile_args[@]}"}
  exit 0
fi

if [[ "$mode" == "build" ]]; then sam build ${profile_args[@]+"${profile_args[@]}"}; exit 0; fi

[[ -f deploy.env ]] || { echo "deploy.env missing: copy deploy.env.example and fill it in"; exit 1; }
set -a; . ./deploy.env; set +a

die() { echo "ERROR: $*" >&2; exit 1; }

# --- always required ---------------------------------------------------------------------
missing=()
for v in SYTELINE_WSDL SYTELINE_USER SYTELINE_SECRET_ARN; do
  [[ -n "${!v:-}" ]] || missing+=("$v")
done
if (( ${#missing[@]} )); then die "missing in deploy.env: ${missing[*]}"; fi

# --- host network: supply all four identifiers, or none to create the network -----------
net_vars=(WORKER_VPC_ID WORKER_SUBNET_IDS WORKER_SG_IDS WORKER_ROUTE_TABLE_IDS)
given=(); absent=()
for v in "${net_vars[@]}"; do
  if [[ -n "${!v:-}" ]]; then given+=("$v"); else absent+=("$v"); fi
done
if (( ${#given[@]} == 0 )); then
  network_mode="create"
elif (( ${#absent[@]} == 0 )); then
  network_mode="supply"
else
  die "supply all four of ${net_vars[*]}, or none of them to create the host network (missing: ${absent[*]})"
fi

api_endpoint_option="${CREATE_API_ENDPOINT:-false}"
[[ "$api_endpoint_option" == "true" || "$api_endpoint_option" == "false" ]] || die "CREATE_API_ENDPOINT must be true or false"

if [[ "$network_mode" == "create" ]]; then
  [[ -n "${HOST_NETWORK_CIDR:-}" ]] || die "HOST_NETWORK_CIDR is required when the host network is created (no default; it must not overlap the plant network)"
  [[ -n "${PLANT_CIDR_1:-}" ]] || die "PLANT_CIDR_1 is required when the host network is created"
  echo "Host network: will be CREATED (${HOST_NETWORK_CIDR}). Reminder: confirm this range does not overlap the plant network"
  echo "or any network you will connect it to; the plant connection itself is not created (see docs/DEPLOYMENT.md)."
else
  ignored=()
  for v in HOST_NETWORK_CIDR PLANT_CIDR_1 PLANT_CIDR_2 PLANT_CIDR_3 DYNAMODB_PREFIX_LIST_ID; do
    [[ -z "${!v:-}" ]] || ignored+=("$v")
  done
  if (( ${#ignored[@]} )); then echo "WARNING: existing network supplied; these settings are ignored: ${ignored[*]}"; fi
  echo "Host network: using the supplied network (nothing network-related is created)."
fi

# --- API-caller endpoint ------------------------------------------------------------------
if [[ "$api_endpoint_option" == "true" ]]; then
  [[ -z "${API_VPCE_ID:-}" ]] || die "CREATE_API_ENDPOINT=true cannot be combined with an existing API_VPCE_ID"
  [[ "$network_mode" == "create" ]] || die "CREATE_API_ENDPOINT=true only works while the host network is created; supply API_VPCE_ID instead"
else
  [[ -n "${API_VPCE_ID:-}" ]] || die "API_VPCE_ID is required (or set CREATE_API_ENDPOINT=true while creating the host network)"
fi

# --- do not silently switch an existing stack between create and supply ------------------
stack_name="$(sed -n 's/^stack_name *= *"\(.*\)"/\1/p' samconfig.toml | head -1)"
region="$(sed -n 's/^region *= *"\(.*\)"/\1/p' samconfig.toml | head -1)"
if command -v aws >/dev/null; then
  status="$(aws ${profile_args[@]+"${profile_args[@]}"} cloudformation describe-stacks --stack-name "$stack_name" --region "$region" \
              --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
  if [[ -n "$status" && "$status" != "None" ]]; then
    host_vpc="$(aws ${profile_args[@]+"${profile_args[@]}"} cloudformation describe-stacks --stack-name "$stack_name" --region "$region" \
                  --query "Stacks[0].Outputs[?OutputKey=='HostVpcId'].OutputValue | [0]" --output text 2>/dev/null || true)"
    existing_mode="supply"
    [[ -n "$host_vpc" && "$host_vpc" != "None" ]] && existing_mode="create"
    if [[ "$existing_mode" != "$network_mode" ]]; then
      echo "Stack $stack_name was deployed in '$existing_mode' host-network mode; this deployment asks for '$network_mode'."
      echo "That would REPLACE the network the functions run in (and remove or abandon the old one)."
      [[ "$allow_network_change" == "true" ]] || die "refusing; re-run with --allow-network-change if you really mean it"
      echo "WARNING: --allow-network-change given; continuing."
    fi
  fi
else
  echo "WARNING: aws CLI not found; cannot check whether the existing stack uses the other host-network mode."
fi

# --- live ERP writes stay off unless explicitly approved ---------------------------------
live="${LIVE_WRITES:-false}"
if [[ "$live" == "true" ]]; then
  [[ -n "${LIVE_APPROVAL_REF:-}" ]] || die "LIVE_WRITES=true requires LIVE_APPROVAL_REF (the ERP owner's approval reference)"
  echo "WARNING: deploying with LIVE ERP WRITES ENABLED (approval ref: $LIVE_APPROVAL_REF). Set LIVE_WRITES=false and redeploy afterwards."
else
  live="false"
fi

sam build ${profile_args[@]+"${profile_args[@]}"}

# Every value is double-quoted so commas (list parameters) and other characters are kept literally.
# (Verified with SAM's own parser: Key="a,b" -> a,b.)
q() { printf '%s="%s"' "$1" "$2"; }

overrides=(
  "$(q SytelineWsdl "$SYTELINE_WSDL")"
  "$(q SytelineUser "$SYTELINE_USER")"
  "$(q SytelineSecretArn "$SYTELINE_SECRET_ARN")"
  "$(q SytelineConfig "${SYTELINE_CONFIG:-CCT}")"
  "$(q DefaultWhse "${SYTELINE_DEFAULT_WHSE:-}")"
  "$(q DefaultOrdType "${SYTELINE_DEFAULT_ORDTYPE:-}")"
  "$(q LiveWrites "$live")"
  "$(q CreateApiEndpoint "$api_endpoint_option")"
)
[[ -z "${API_VPCE_ID:-}" ]] || overrides+=("$(q ApiVpcEndpointId "$API_VPCE_ID")")

if [[ "$network_mode" == "supply" ]]; then
  overrides+=(
    "$(q WorkerVpcId "$WORKER_VPC_ID")"
    "$(q WorkerSubnetIds "$WORKER_SUBNET_IDS")"
    "$(q WorkerSecurityGroupIds "$WORKER_SG_IDS")"
    "$(q WorkerRouteTableIds "$WORKER_ROUTE_TABLE_IDS")"
    "$(q CreateVpcEndpoints "${CREATE_VPC_ENDPOINTS:-true}")"
  )
else
  overrides+=(
    "$(q HostNetworkCidr "$HOST_NETWORK_CIDR")"
    "$(q PlantCidr1 "$PLANT_CIDR_1")"
  )
  [[ -z "${PLANT_CIDR_2:-}" ]] || overrides+=("$(q PlantCidr2 "$PLANT_CIDR_2")")
  [[ -z "${PLANT_CIDR_3:-}" ]] || overrides+=("$(q PlantCidr3 "$PLANT_CIDR_3")")
  [[ -z "${DYNAMODB_PREFIX_LIST_ID:-}" ]] || overrides+=("$(q DynamoDbPrefixListId "$DYNAMODB_PREFIX_LIST_ID")")
fi

sam deploy ${profile_args[@]+"${profile_args[@]}"} --parameter-overrides "${overrides[@]}"
