#!/usr/bin/env bash
# Build and deploy the SyteLine integration stack (us-gov-east-1, stack "sytline-connect"): ONE Lambda function attached to an
# EXISTING VPC, subnets and security group(s). This script and the template create no network resource.
#   ./deploy.sh                          build + deploy (interactive change-set confirmation)
#   ./deploy.sh --build                  build only
#   ./deploy.sh --validate               lint the template only
#   ./deploy.sh --profile NAME           use this AWS credentials profile (flag > AWS_PROFILE_NAME in the settings file > default chain)
#   ./deploy.sh --yes                    do not ask before writing the database settings into the credentials secret
# Flags can be combined, e.g. ./deploy.sh --profile govcloud-prod --validate
# Settings come from deploy.env (copy deploy.env.example; git-ignored). The database login (SQL_SERVER, SQL_DATABASE, SQL_USER,
# SQL_PASS, SQL_PORT, SQL_CA_PEM_FILE) may be given there too: it is MERGED into the existing credentials secret
# (SYTELINE_SECRET_ARN) before deploying, never passed to CloudFormation and never printed.
set -euo pipefail
cd "$(dirname "$0")"

mode="deploy"
profile=""
assume_yes="false"
while (( $# )); do
  case "$1" in
    --build) mode="build" ;;
    --validate) mode="validate" ;;
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
  # the whole ARN, so a placeholder or a stray space/carriage return is named here instead of as "Invalid name" by AWS
  [[ "$arn" =~ ^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]+:secret:[A-Za-z0-9/_+=.@-]+$ ]] \
    || die "SYTELINE_SECRET_ARN is not a complete secret ARN (arn:...:secretsmanager:REGION:ACCOUNT-DIGITS:secret:NAME-SUFFIX; no spaces or quotes, and the account id must not be the ACCOUNT placeholder)"
  echo "Will add or update these keys in the credentials secret: ${keys[*]}. All other keys stay as they are, and values are never printed."
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
# The deployment: one function in the existing network
# =========================================================================================
# --- always required ---------------------------------------------------------------------
missing=()
for v in SYTELINE_WSDL SYTELINE_USER SYTELINE_SECRET_ARN WORKER_VPC_ID WORKER_SUBNET_IDS WORKER_SG_IDS; do
  [[ -n "${!v:-}" ]] || missing+=("$v")
done
if (( ${#missing[@]} )); then die "missing in $env_file: ${missing[*]}"; fi

# --- the existing network: nothing here is created ----------------------------------------
[[ "$WORKER_VPC_ID" =~ ^vpc-[0-9a-f]+$ ]] || die "WORKER_VPC_ID must look like vpc-0123abcd"
[[ "$WORKER_SUBNET_IDS" =~ ^subnet-[0-9a-f]+(,subnet-[0-9a-f]+)*$ ]] || die "WORKER_SUBNET_IDS must be comma-separated subnet-... ids, no spaces"
[[ "$WORKER_SG_IDS" =~ ^sg-[0-9a-f]+(,sg-[0-9a-f]+)*$ ]] || die "WORKER_SG_IDS must be comma-separated sg-... ids, no spaces"
# the private API is optional: with an existing execute-api interface endpoint it is created, without one there is no API
if [[ -n "${API_VPCE_ID:-}" ]]; then
  [[ "$API_VPCE_ID" =~ ^vpce-[0-9a-f]+$ ]] || die "API_VPCE_ID must look like vpce-0123abcd (an existing execute-api interface endpoint)"
  echo "API: the private API will be created and made reachable through endpoint $API_VPCE_ID."
else
  echo "API: API_VPCE_ID is empty, so NO API is created; the function is reached by direct invocation, the IDP event and its own schedule only."
fi
if [[ "$WORKER_SUBNET_IDS" != *,* ]]; then
  echo "WARNING: only one subnet is given (WORKER_SUBNET_IDS): the function is not highly available. Use subnets in two Availability Zones."
fi
echo "Network: the function will be attached to VPC $WORKER_VPC_ID (subnets $WORKER_SUBNET_IDS, security groups $WORKER_SG_IDS); nothing network-related is created."
echo "Reminder: this network must give the function a route to the ERP and the database and a private path to Secrets Manager, DynamoDB and the IDP; the stack creates none of them. See docs/DEPLOYMENT.md."

# settings of the earlier shapes (create-the-network) are no longer used: say so, never pass them
ignored=()
for v in HOST_NETWORK_CIDR PLANT_CIDR_1 PLANT_CIDR_2 PLANT_CIDR_3 DYNAMODB_PREFIX_LIST_ID CREATE_VPC_ENDPOINTS \
         CREATE_API_ENDPOINT WORKER_ROUTE_TABLE_IDS ATTACH_VPC_ID ATTACH_SUBNET_IDS ATTACH_SG_IDS ALLOW_PLAIN_HTTP \
         VERIFY_TLS DATABASE_CHECK; do
  [[ -z "${!v:-}" ]] || ignored+=("$v")
done
if (( ${#ignored[@]} )); then echo "Note: no longer used by this deployment, ignored (remove them from $env_file): ${ignored[*]}"; fi

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
[[ "${LOG_EVENTS:-false}" == "true" || "${LOG_EVENTS:-false}" == "false" ]] || die "LOG_EVENTS must be true or false"
if [[ "${LOG_EVENTS:-false}" == "true" ]]; then
  echo "WARNING: LOG_EVENTS=true: the function logs the WHOLE incoming event, so its logs will hold document and order content (names, addresses, part numbers). Turn it off (LOG_EVENTS=false) and redeploy when you have finished debugging."
fi
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
# IDP_STATE_MACHINE_ARN: when given, the IDP's finished-document events start the event intake (always passed: empty clears it)
[[ -z "${IDP_STATE_MACHINE_ARN:-}" || "$IDP_STATE_MACHINE_ARN" =~ ^arn:aws[a-z-]*:states:[a-z0-9-]+:[0-9]+:stateMachine:[A-Za-z0-9_-]+$ ]] \
  || die "IDP_STATE_MACHINE_ARN is not a Step Functions state machine ARN (arn:...:states:REGION:ACCOUNT:stateMachine:NAME)"
# IDP_KMS_KEY_ARNS: the customer-managed key(s) that encrypt the IDP's bucket and table (comma-separated; always passed)
if [[ -n "${IDP_KMS_KEY_ARNS:-}" ]]; then
  IFS=',' read -r -a kms_keys <<< "$IDP_KMS_KEY_ARNS"
  for k in "${kms_keys[@]}"; do
    [[ "$k" =~ ^arn:aws[a-z-]*:kms:[a-z0-9-]+:[0-9]+:key/[0-9a-fA-F-]+$ ]] \
      || die "IDP_KMS_KEY_ARNS: '$k' is not a KMS key ARN (arn:...:kms:REGION:ACCOUNT:key/ID), comma-separated, no spaces"
  done
fi
if [[ -n "${IDP_STATE_MACHINE_ARN:-}" && -z "${IDP_STACK_NAME:-}" ]]; then
  die "IDP_STATE_MACHINE_ARN needs IDP_STACK_NAME (and the other IDP settings): the event intake reads the document through the IDP SDK"
fi


# --- an existing stack of the earlier shape (several functions) is replaced in place: say what to expect --------
stack_name="$(sed -n 's/^stack_name *= *"\(.*\)"/\1/p' samconfig.toml | head -1)"
region="$(sed -n 's/^region *= *"\(.*\)"/\1/p' samconfig.toml | head -1)"
if command -v aws >/dev/null; then
  others="$(aws ${profile_args[@]+"${profile_args[@]}"} cloudformation list-stack-resources --stack-name "$stack_name" --region "$region" \
              --query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function' && PhysicalResourceId!='${stack_name}-fn'].PhysicalResourceId" \
              --output text 2>/dev/null || true)"
  if [[ -n "$others" && "$others" != "None" ]]; then
    echo "NOTICE: stack $stack_name still has functions of the earlier shape ($others). This deployment replaces them with the one function"
    echo "        '${stack_name}-fn' and keeps the jobs table. If that stack created its own VPC, read docs/MIGRATION.md BEFORE confirming the change set."
  fi
fi

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
  "$(q WorkerVpcId "$WORKER_VPC_ID")"
  "$(q WorkerSubnetIds "$WORKER_SUBNET_IDS")"
  "$(q WorkerSecurityGroupIds "$WORKER_SG_IDS")"
  "$(q ApiVpcEndpointId "${API_VPCE_ID:-}")"   # always passed: empty = no API (and clears an earlier endpoint)
  # always passed (empty clears a value an earlier deployment set: sam keeps old values for parameters it is not given)
  # which route each ERP operation uses: an IDO name = the web service, `database` = the SyteLine database (always passed)
  "$(q NextCoIdo "${NEXT_CO_IDO:-database}")"
  "$(q NextJobIdo "${NEXT_JOB_IDO:-database}")"
  "$(q CustomerIdo "${CUSTOMER_IDO:-database}")"
  "$(q DefaultsIdo "${DEFAULTS_IDO:-database}")"
  "$(q SqlEncrypt "${SQL_ENCRYPT:-true}")"
  "$(q LogEvents "${LOG_EVENTS:-false}")"
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
  "$(q IdpKmsKeyArns "${IDP_KMS_KEY_ARNS:-}")"
  "$(q IdpStateMachineArn "${IDP_STATE_MACHINE_ARN:-}")"
)

sam deploy ${profile_args[@]+"${profile_args[@]}"} --parameter-overrides "${overrides[@]}"
