#!/usr/bin/env bash
# Build and deploy the SyteLine integration stack (us-gov-east-1, stack "sytline-connect").
#   ./deploy.sh                          build + deploy (interactive change-set confirmation)
#   ./deploy.sh --build                  build only
#   ./deploy.sh --validate               lint the template only
#   ./deploy.sh --allow-network-change   permit switching between "create" and "supply" host-network mode
#   ./deploy.sh --profile NAME           use this AWS credentials profile (flag > AWS_PROFILE_NAME in the settings file > default chain)
#   ./deploy.sh --yes                    do not ask before writing the database settings into the credentials secret
#   ./deploy.sh --standalone             the STAND-ALONE (no VPC) deployment: stack "sytline-connect-standalone",
#                                        same deploy.env (its STAND-ALONE ONLY block; https:// ERP required)
# Flags can be combined, e.g. ./deploy.sh --profile govcloud-prod --validate
# Settings come from deploy.env (copy deploy.env.example; git-ignored) in both modes: shared settings once, then a PLANT ONLY
# block and a STAND-ALONE ONLY block; each mode ignores the other's block.
# The database login (SQL_SERVER, SQL_DATABASE, SQL_USER, SQL_PASS, SQL_PORT, SQL_CA_PEM_FILE) may be given there too: it is MERGED
# into the existing credentials secret (SYTELINE_SECRET_ARN) before deploying, never passed to CloudFormation and never printed.
set -euo pipefail
cd "$(dirname "$0")"

mode="deploy"
allow_network_change="false"
profile=""
standalone="false"
assume_yes="false"
while (( $# )); do
  case "$1" in
    --build) mode="build" ;;
    --validate) mode="validate" ;;
    --allow-network-change) allow_network_change="true" ;;
    --standalone) standalone="true" ;;
    --yes) assume_yes="true" ;;
    --profile) [[ $# -ge 2 && -n "${2:-}" && "${2:0:2}" != "--" ]] || { echo "ERROR: --profile needs a profile name" >&2; exit 2; }
               profile="$2"; shift ;;
    --profile=*) profile="${1#--profile=}"; [[ -n "$profile" ]] || { echo "ERROR: --profile needs a profile name" >&2; exit 2; } ;;
    *) echo "unknown argument: $1"; exit 2 ;;
  esac
  shift
done

# the settings file is optional for --build/--validate; read it early only for AWS_PROFILE_NAME
env_file="deploy.env"
cfg_args=()
if [[ "$standalone" == "true" ]]; then
  if [[ -f deploy.standalone.env ]]; then
    echo "ERROR: deploy.standalone.env is no longer read: move its settings into deploy.env (stand-alone block) and delete it" >&2
    exit 1
  fi
  cfg_args=(--config-env standalone)   # separate stack, template and build directory (samconfig.toml)
fi
if [[ -z "$profile" && -f "$env_file" ]]; then
  profile="$(set -a; . "./$env_file"; printf '%s' "${AWS_PROFILE_NAME:-}")"
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
  sam validate --lint ${cfg_args[@]+"${cfg_args[@]}"} ${profile_args[@]+"${profile_args[@]}"}
  exit 0
fi

if [[ "$mode" == "build" ]]; then sam build ${cfg_args[@]+"${cfg_args[@]}"} ${profile_args[@]+"${profile_args[@]}"}; exit 0; fi

[[ -f "$env_file" ]] || { echo "$env_file missing: copy $env_file.example and fill it in"; exit 1; }
set -a; . "./$env_file"; set +a

die() { echo "ERROR: $*" >&2; exit 1; }

# --- database settings -> credentials secret --------------------------------------------------------
# Merges the database values of the settings file into the JSON secret named by SYTELINE_SECRET_ARN (other keys are kept).
# The values travel through the environment and a 0600 temporary file only: not through the command line, CloudFormation
# parameters or the output. Asks before writing unless --yes; with values set and no terminal, it refuses rather than guessing.
update_secret() {
  local keys=() k
  for k in SQL_SERVER SQL_DATABASE SQL_USER SQL_PASS SQL_PORT; do [[ -z "${!k:-}" ]] || keys+=("$k"); done
  if [[ -n "${SQL_CA_PEM_FILE:-}" ]]; then
    [[ -f "$SQL_CA_PEM_FILE" ]] || die "SQL_CA_PEM_FILE does not exist: $SQL_CA_PEM_FILE"
    keys+=("SQL_CA_PEM")
  fi
  (( ${#keys[@]} )) || return 0
  [[ -z "${SQL_PORT:-}" || "$SQL_PORT" =~ ^[0-9]+$ ]] || die "SQL_PORT must be a whole number"
  command -v aws >/dev/null || die "aws CLI not found: it is needed to update the credentials secret"
  command -v python3 >/dev/null || die "python3 not found: it is needed to merge the database values into the secret"
  local arn="${SYTELINE_SECRET_ARN:-}" region
  [[ "$arn" =~ ^arn:aws[a-z-]*:secretsmanager:([a-z0-9-]+): ]] || die "SYTELINE_SECRET_ARN is not a Secrets Manager ARN"
  region="${BASH_REMATCH[1]}"
  echo "The database settings in $env_file will be MERGED into the credentials secret (keys: ${keys[*]}); its other keys are kept. Values are never printed."
  if [[ "$assume_yes" != "true" ]]; then
    if [[ -t 0 ]]; then
      local answer; read -r -p "Write them to the secret now? [y/N] " answer
      [[ "$answer" =~ ^[Yy]$ ]] || die "not confirmed: the secret was not changed and nothing was deployed (--yes skips this question)"
    else
      die "database settings are set but there is no terminal to confirm: re-run with --yes to write them to the secret"
    fi
  fi
  local current tmp status=0
  current="$(aws secretsmanager get-secret-value --secret-id "$arn" --region "$region" ${profile_args[@]+"${profile_args[@]}"} \
               --query SecretString --output text)" || die "could not read the credentials secret (permissions or the ARN?)"
  tmp="$(mktemp)"; chmod 600 "$tmp"
  printf '%s' "$current" | KEYS="${keys[*]}" python3 -c '
import json, os, sys
cur = json.load(sys.stdin)
if not isinstance(cur, dict):
    sys.exit(2)
new = dict(cur)
for key in os.environ["KEYS"].split():
    new[key] = open(os.environ["SQL_CA_PEM_FILE"]).read() if key == "SQL_CA_PEM" else os.environ[key]
if new == cur:
    sys.exit(3)
json.dump(new, open(sys.argv[1], "w"))
' "$tmp" || status=$?
  if (( status == 3 )); then rm -f "$tmp"; echo "The credentials secret already holds these database settings; nothing to write."; return 0; fi
  if (( status != 0 )); then rm -f "$tmp"; die "the credentials secret is not a JSON object, so the database settings were not merged"; fi
  aws secretsmanager put-secret-value --secret-id "$arn" --region "$region" ${profile_args[@]+"${profile_args[@]}"} \
      --secret-string "file://$tmp" >/dev/null || { rm -f "$tmp"; die "could not write the credentials secret"; }
  rm -f "$tmp"
  echo "Credentials secret updated (keys: ${keys[*]}). You may remove the database values from $env_file now; they are not needed again."
}

# =========================================================================================
# Stand-alone (no VPC) deployment
# =========================================================================================
if [[ "$standalone" == "true" ]]; then
  missing=()
  for v in SYTELINE_WSDL SYTELINE_USER SYTELINE_SECRET_ARN; do
    [[ -n "${!v:-}" ]] || missing+=("$v")
  done
  if (( ${#missing[@]} )); then die "missing in $env_file: ${missing[*]}"; fi

  # optional attachment to an EXISTING network: all three settings, or none (no VPC)
  att_vars=(ATTACH_VPC_ID ATTACH_SUBNET_IDS ATTACH_SG_IDS)
  att_given=(); att_absent=()
  for v in "${att_vars[@]}"; do
    if [[ -n "${!v:-}" ]]; then att_given+=("$v"); else att_absent+=("$v"); fi
  done
  if (( ${#att_given[@]} > 0 && ${#att_absent[@]} > 0 )); then
    die "supply all three of ${att_vars[*]}, or none of them to run outside any VPC (missing: ${att_absent[*]})"
  fi
  if (( ${#att_given[@]} == 3 )); then
    [[ "$ATTACH_VPC_ID" =~ ^vpc-[0-9a-f]+$ ]] || die "ATTACH_VPC_ID must look like vpc-0123abcd"
    [[ "$ATTACH_SUBNET_IDS" =~ ^subnet-[0-9a-f]+(,subnet-[0-9a-f]+)*$ ]] || die "ATTACH_SUBNET_IDS must be comma-separated subnet-... ids, no spaces"
    [[ "$ATTACH_SG_IDS" =~ ^sg-[0-9a-f]+(,sg-[0-9a-f]+)*$ ]] || die "ATTACH_SG_IDS must be comma-separated sg-... ids, no spaces"
    attached="true"
  else
    attached="false"
  fi

  # optional database connectivity check of the health action (feature 005): needs the VPC attachment
  database_check="${DATABASE_CHECK:-auto}"
  [[ "$database_check" =~ ^(auto|true|false)$ ]] || die "DATABASE_CHECK must be auto, true or false"
  [[ "${SQL_ENCRYPT:-true}" =~ ^(true|false)$ ]] || die "SQL_ENCRYPT must be true or false"
  [[ "${SQL_VERIFY_HOSTNAME:-true}" =~ ^(true|false)$ ]] || die "SQL_VERIFY_HOSTNAME must be true or false"
  [[ "${SQL_LOGIN_TIMEOUT:-5}" =~ ^[0-9]+$ ]] || die "SQL_LOGIN_TIMEOUT must be a whole number"
  if [[ "$database_check" == "true" && "$attached" != "true" ]]; then
    die "DATABASE_CHECK=true needs ATTACH_VPC_ID, ATTACH_SUBNET_IDS and ATTACH_SG_IDS (outside a VPC there is no route to the plant database)"
  fi
  if [[ "${SQL_ENCRYPT:-true}" == "false" ]]; then
    echo "WARNING: SQL_ENCRYPT=false: the database connection will be UNENCRYPTED; the database login and password cross the network unprotected."
  fi

  # the plant network settings share the file but do not apply to this version: not read, not passed
  na=()
  for v in WORKER_VPC_ID WORKER_SUBNET_IDS WORKER_SG_IDS WORKER_ROUTE_TABLE_IDS HOST_NETWORK_CIDR PLANT_CIDR_1 \
           PLANT_CIDR_2 PLANT_CIDR_3 DYNAMODB_PREFIX_LIST_ID API_VPCE_ID CREATE_API_ENDPOINT CREATE_VPC_ENDPOINTS; do
    [[ -z "${!v:-}" ]] || na+=("$v")
  done
  if (( ${#na[@]} )); then echo "Note: ignored by the stand-alone version (it only attaches to an existing VPC, subnets and security group): ${na[*]}"; fi

  # ERP address: the scheme decides which template parameter carries it; http:// needs the explicit override
  [[ "$SYTELINE_WSDL" =~ ^https?://[^/]*@ ]] && die "SYTELINE_WSDL must not contain credentials (user:password@host)"
  allow_http="${ALLOW_PLAIN_HTTP:-false}"
  [[ "$allow_http" == "true" || "$allow_http" == "false" ]] || die "ALLOW_PLAIN_HTTP must be true or false"
  [[ "${USE_FIPS_ENDPOINTS:-true}" == "true" || "${USE_FIPS_ENDPOINTS:-true}" == "false" ]] || die "USE_FIPS_ENDPOINTS must be true or false"
  verify_tls="${VERIFY_TLS:-true}"
  [[ "$verify_tls" == "true" || "$verify_tls" == "false" ]] || die "VERIFY_TLS must be true or false"
  wsdl_param=""
  case "$SYTELINE_WSDL" in
    https://*) wsdl_param="SytelineWsdlHttps" ;;
    http://*)
      [[ "$allow_http" == "true" ]] || die "SYTELINE_WSDL is plain http://. The stand-alone version requires https:// (set ALLOW_PLAIN_HTTP=true only if you accept sending the ERP password unencrypted)"
      wsdl_param="SytelineWsdlHttp"
      echo "WARNING: PLAIN HTTP OVERRIDE IS ON. The ERP password will cross the network UNENCRYPTED."
      ;;
    *) die "SYTELINE_WSDL must start with https:// (or http:// with ALLOW_PLAIN_HTTP=true)" ;;
  esac
  [[ "$allow_http" == "false" ]] || echo "WARNING: ALLOW_PLAIN_HTTP=true: https is no longer enforced for the ERP connection."

  live="${LIVE_WRITES:-false}"
  if [[ "$live" == "true" ]]; then
    [[ -n "${LIVE_APPROVAL_REF:-}" ]] || die "LIVE_WRITES=true requires LIVE_APPROVAL_REF (the ERP owner's approval reference)"
    echo "WARNING: deploying with LIVE ERP WRITES ENABLED (approval ref: $LIVE_APPROVAL_REF). Set LIVE_WRITES=false and redeploy afterwards."
  else
    live="false"
  fi

  q() { printf '%s="%s"' "$1" "$2"; }
  # sam deploy keeps an existing stack's value for any parameter not given, so clear the address type not in use
  # (otherwise a stack first deployed with https:// still has it set when you switch to http://, and the
  # "exactly one" rule refuses the deployment)
  other_wsdl_param="SytelineWsdlHttp"; [[ "$wsdl_param" == "SytelineWsdlHttp" ]] && other_wsdl_param="SytelineWsdlHttps"
  attach_overrides=()
  if [[ "$attached" == "true" ]]; then
    attach_overrides=("$(q AttachVpcId "$ATTACH_VPC_ID")" "$(q AttachSubnetIds "$ATTACH_SUBNET_IDS")" "$(q AttachSecurityGroupIds "$ATTACH_SG_IDS")")
  fi
  if [[ "$attached" == "true" ]]; then
    echo "Deploying the STAND-ALONE version ATTACHED to existing VPC $ATTACH_VPC_ID (stack sytline-connect-standalone). No network resources are created."
    echo "Reminder: this network must give the functions a route to the ERP and a private path to Secrets Manager (an endpoint or a NAT route)."
  else
    echo "Deploying the STAND-ALONE version (no VPC): stack sytline-connect-standalone. No network resources are created."
  fi
  update_secret
  sam build "${cfg_args[@]}" ${profile_args[@]+"${profile_args[@]}"}
  sam deploy "${cfg_args[@]}" ${profile_args[@]+"${profile_args[@]}"} --parameter-overrides \
    "$(q "$wsdl_param" "$SYTELINE_WSDL")" \
    "$(q "$other_wsdl_param" "")" \
    "$(q AllowPlainHttp "$allow_http")" \
    "$(q VerifyTls "$verify_tls")" \
    "$(q SytelineUser "$SYTELINE_USER")" \
    "$(q SytelineSecretArn "$SYTELINE_SECRET_ARN")" \
    "$(q SytelineConfig "${SYTELINE_CONFIG:-CCT}")" \
    "$(q LiveWrites "$live")" \
    "$(q UseFipsEndpoints "${USE_FIPS_ENDPOINTS:-true}")" \
    "$(q DatabaseCheck "$database_check")" \
    "$(q SqlEncrypt "${SQL_ENCRYPT:-true}")" \
    "$(q SqlVerifyHostname "${SQL_VERIFY_HOSTNAME:-true}")" \
    "$(q SqlLoginTimeout "${SQL_LOGIN_TIMEOUT:-5}")" \
    ${attach_overrides[@]+"${attach_overrides[@]}"}
  exit 0
fi

# --- always required ---------------------------------------------------------------------
missing=()
for v in SYTELINE_WSDL SYTELINE_USER SYTELINE_SECRET_ARN; do
  [[ -n "${!v:-}" ]] || missing+=("$v")
done
if (( ${#missing[@]} )); then die "missing in $env_file: ${missing[*]}"; fi

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

# Job-number workflow: where the PO details are read from (an IDP accelerator stack). IDP_REPO is the checkout whose
# idp_sdk and idp_common are packaged with the functions (never installed by name: `idp-common` on PyPI is not ours).
[[ "${SQL_ENCRYPT:-true}" == "true" || "${SQL_ENCRYPT:-true}" == "false" ]] || die "SQL_ENCRYPT must be true or false"
if [[ "${SQL_ENCRYPT:-true}" == "false" ]]; then
  echo "WARNING: SQL_ENCRYPT=false: the database connection will be UNENCRYPTED; the database login and password cross the network unprotected."
fi
for flag in SQL_VERIFY_HOSTNAME USE_FIPS_ENDPOINTS; do
  [[ "${!flag:-true}" == "true" || "${!flag:-true}" == "false" ]] || die "$flag must be true or false"
done
for num in SQL_LOGIN_TIMEOUT SQL_TIMEOUT SITE_ID MATCH_CAP; do
  [[ "${!num:-0}" =~ ^[0-9]+$ ]] || die "$num must be a whole number"
done
for num in MATCH_MIN_SCORE MATCH_MARGIN; do
  [[ "${!num:-0}" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "$num must be a number"
done
# IDP_FIELD_MAP is JSON: its double quotes are escaped so sam's parameter parser keeps them (checked with sam's own parser)
idp_field_map="${IDP_FIELD_MAP:-}"
if [[ -n "$idp_field_map" ]]; then
  # deploy.env is sourced as shell: the value must be single-quoted there, or its double quotes are lost
  if command -v python3 >/dev/null; then
    printf '%s' "$idp_field_map" | python3 -c 'import json,sys; assert isinstance(json.load(sys.stdin), dict)' 2>/dev/null \
      || die "IDP_FIELD_MAP must be a JSON object in single quotes, e.g. IDP_FIELD_MAP='{\"po_number\":\"purchase_order\"}'"
  else
    [[ "$idp_field_map" == \{*\} ]] || die "IDP_FIELD_MAP must be a JSON object"
  fi
  idp_field_map="${idp_field_map//\"/\\\"}"
fi
if [[ -n "${IDP_STACK_NAME:-}" ]]; then
  [[ -n "${IDP_OUTPUT_BUCKET:-}" && -n "${IDP_TRACKING_TABLE:-}" ]] || die "IDP_STACK_NAME needs IDP_OUTPUT_BUCKET and IDP_TRACKING_TABLE too"
  [[ -n "${IDP_REPO:-}" ]] || echo "WARNING: IDP_REPO is not set: the IDP SDK will not be packaged, so reserve and save will answer IDP_UNAVAILABLE."
else
  echo "Note: IDP_STACK_NAME is empty: the job-number workflow (reserve / save) will answer IDP_UNAVAILABLE."
fi
[[ -z "${IDP_REPO:-}" ]] || export IDP_REPO

update_secret
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
  # always passed (empty clears a value an earlier deployment set: sam keeps old values for parameters it is not given)
  # which route each ERP operation uses: an IDO name = the web service, `database` = the SyteLine database (always passed)
  "$(q NextCoIdo "${NEXT_CO_IDO:-database}")"
  "$(q NextJobIdo "${NEXT_JOB_IDO:-database}")"
  "$(q CustomerIdo "${CUSTOMER_IDO:-database}")"
  "$(q DefaultsIdo "${DEFAULTS_IDO:-database}")"
  "$(q SqlEncrypt "${SQL_ENCRYPT:-true}")"
  "$(q SqlVerifyHostname "${SQL_VERIFY_HOSTNAME:-true}")"
  "$(q SqlLoginTimeout "${SQL_LOGIN_TIMEOUT:-5}")"
  "$(q SqlTimeout "${SQL_TIMEOUT:-10}")"
  "$(q SiteId "${SITE_ID:-10}")"
  "$(q MatchMinScore "${MATCH_MIN_SCORE:-70}")"
  "$(q MatchMargin "${MATCH_MARGIN:-3}")"
  "$(q MatchCap "${MATCH_CAP:-1000}")"
  "$(q UseFipsEndpoints "${USE_FIPS_ENDPOINTS:-true}")"
  "$(q IdpDocumentClass "${IDP_DOCUMENT_CLASS:-}")"
  "$(q IdpFieldMap "$idp_field_map")"
  "$(q IdpStackName "${IDP_STACK_NAME:-}")"
  "$(q IdpRegion "${IDP_REGION:-}")"
  "$(q IdpOutputBucketName "${IDP_OUTPUT_BUCKET:-}")"
  "$(q IdpTrackingTableName "${IDP_TRACKING_TABLE:-}")"
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
