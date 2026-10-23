#!/usr/bin/env bash
# Invoke the SyteLine Lambda with a payload read from a file, and print the response.
#   ./invoke.sh PAYLOAD_FILE                      invoke <stack>-fn with the file's JSON as the payload
#   ./invoke.sh --check PAYLOAD_FILE              only look at the file (valid JSON? what are its keys?); nothing is invoked
#   ./invoke.sh --profile NAME PAYLOAD_FILE       AWS credentials profile (flag > AWS_PROFILE_NAME in deploy.env > default chain)
#   ./invoke.sh --function NAME PAYLOAD_FILE      another function (default: <stack_name from samconfig.toml>-fn)
#   ./invoke.sh --region R PAYLOAD_FILE           another region (default: the region in samconfig.toml)
#   ./invoke.sh --async PAYLOAD_FILE              Event invocation: returns at once, no response body
#   ./invoke.sh --log PAYLOAD_FILE                also print the tail of the function's log
#   ./invoke.sh --debug PAYLOAD_FILE              add "debug": true to the payload: an IDP failure then shows the SDK's message
# Examples:  ./invoke.sh ../sample_events.json      (the IDP finished-document event; no "action" needed)
#            echo '{"action":"health","dry_run":true}' > /tmp/health.json && ./invoke.sh /tmp/health.json
# Exit status: 0 = invoked and the function did not fail, 1 = the function failed or the invoke failed, 2 = bad usage or payload.
set -euo pipefail
cd "$(dirname "$0")"

profile="" region="" function="" check="false" async="false" want_log="false" debug="false" file=""
while (( $# )); do
  case "$1" in
    --check) check="true" ;;
    --async) async="true" ;;
    --log) want_log="true" ;;
    --debug) debug="true" ;;
    --profile) [[ $# -ge 2 && "${2:0:2}" != "--" ]] || { echo "ERROR: --profile needs a profile name" >&2; exit 2; }; profile="$2"; shift ;;
    --region) [[ $# -ge 2 && "${2:0:2}" != "--" ]] || { echo "ERROR: --region needs a region" >&2; exit 2; }; region="$2"; shift ;;
    --function) [[ $# -ge 2 && "${2:0:2}" != "--" ]] || { echo "ERROR: --function needs a function name" >&2; exit 2; }; function="$2"; shift ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --*) echo "ERROR: unknown option: $1" >&2; exit 2 ;;
    *) [[ -z "$file" ]] || { echo "ERROR: only one payload file is accepted (got '$file' and '$1')" >&2; exit 2; }; file="$1" ;;
  esac
  shift
done
[[ -n "$file" ]] || { echo "ERROR: give the payload file, e.g. ./invoke.sh ../sample_events.json" >&2; exit 2; }
[[ -f "$file" ]] || { echo "ERROR: payload file not found: $file" >&2; exit 2; }
command -v python3 >/dev/null || { echo "ERROR: python3 is needed to check the payload" >&2; exit 2; }

# --- look at the payload before anything is sent -------------------------------------------------------------------------
if ! python3 - "$file" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path) as f:
        payload = json.load(f)
except ValueError as exc:
    print(f"ERROR: {path} is not valid JSON: {exc}", file=sys.stderr)
    print("       (a Python-style dump with single quotes or None is not JSON; see tests/tools/make_sample_event.py)", file=sys.stderr)
    sys.exit(2)
if not isinstance(payload, dict):
    print(f"ERROR: {path} must hold a JSON object, not {type(payload).__name__}", file=sys.stderr)
    sys.exit(2)
detail = payload.get("detail")
print(f"payload: {path}, {len(json.dumps(payload))} bytes, top-level keys: {sorted(payload)}")
if "action" in payload:
    print(f"  action: {payload['action']!r}")
elif "source" in payload or "detail-type" in payload or "detail" in payload:
    print(f"  no action (default document_event); source={payload.get('source')!r} detail-type={payload.get('detail-type')!r} "
          f"detail is {type(detail).__name__}" + (f" with keys {sorted(detail)}" if isinstance(detail, dict) else ""))
else:
    print("  no action: the function will treat this as an IDP event and refuse it; add \"action\" (for example \"health\")")
PY
then exit 2; fi
if [[ "$check" == "true" ]]; then exit 0; fi

# --- where to send it --------------------------------------------------------------------------------------------------
if [[ -z "$profile" && -f deploy.env ]]; then
  profile="$(set -a; . ./deploy.env; printf '%s' "${AWS_PROFILE_NAME:-}")"
fi
stack_name="$(sed -n 's/^stack_name *= *"\(.*\)"/\1/p' samconfig.toml | head -1)"
[[ -n "$region" ]] || region="$(sed -n 's/^region *= *"\(.*\)"/\1/p' samconfig.toml | head -1)"
[[ -n "$function" ]] || function="${stack_name}-fn"
command -v aws >/dev/null || { echo "ERROR: aws CLI not found" >&2; exit 2; }
profile_args=(); [[ -z "$profile" ]] || profile_args=(--profile "$profile")
# AWS CLI v2 wants the payload as a blob: fileb:// sends the file's bytes as they are; v1 has no such option
binary_args=(); [[ "$(aws --version 2>&1)" == aws-cli/2* ]] && binary_args=(--cli-binary-format raw-in-base64-out)
type_args=(); [[ "$async" == "true" ]] && type_args=(--invocation-type Event)
log_args=(); [[ "$want_log" == "true" && "$async" != "true" ]] && log_args=(--log-type Tail)

out="$(mktemp)"; meta="$(mktemp)"; sent="$file"; trap 'rm -f "$out" "$meta" "$sent_tmp"' EXIT
sent_tmp=""
if [[ "$debug" == "true" ]]; then  # a temporary copy of the payload with "debug": true; the file itself is not changed
  sent_tmp="$(mktemp)"
  python3 - "$file" "$sent_tmp" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1]))
payload["debug"] = True
json.dump(payload, open(sys.argv[2], "w"))
PY
  sent="$sent_tmp"
fi
echo "invoking $function in $region${profile:+ (profile $profile)}"
if ! aws lambda invoke ${profile_args[@]+"${profile_args[@]}"} --region "$region" --function-name "$function" \
       ${binary_args[@]+"${binary_args[@]}"} ${type_args[@]+"${type_args[@]}"} ${log_args[@]+"${log_args[@]}"} \
       --payload "fileb://$sent" "$out" >"$meta"; then
  echo "ERROR: the invoke call failed (function name, region, profile or permissions?)" >&2
  exit 1
fi

# --- show the result -----------------------------------------------------------------------------------------------------
python3 - "$meta" "$out" "$async" <<'PY'
import base64, json, sys
meta_path, out_path, is_async = sys.argv[1:4]
try:
    meta = json.load(open(meta_path))
except ValueError:
    meta = {}
print(f"status: {meta.get('StatusCode')}" + (f", function error: {meta['FunctionError']}" if meta.get("FunctionError") else ""))
if meta.get("LogResult"):
    print("--- log tail ---")
    print(base64.b64decode(meta["LogResult"]).decode("utf-8", "replace").rstrip())
    print("--- response ---")
if is_async != "true":
    raw = open(out_path).read()
    try:
        print(json.dumps(json.loads(raw), indent=2))
    except ValueError:
        print(raw)
sys.exit(1 if meta.get("FunctionError") else 0)
PY
