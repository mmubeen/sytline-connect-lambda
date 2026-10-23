# Quickstart: validating the stand-alone deployment

Steps 1-4 run offline; nothing deploys unless a step says so.

1. **Static checks** (`cd src`): `cfn-lint template-standalone.yaml`; `sam validate --lint --region us-gov-east-1 -t template-standalone.yaml`; `./deploy.sh --standalone --validate`.
2. **Template tests**: `pytest tests/unit/test_template_standalone.py` — no `VpcConfig`, no `AWS::EC2::*` resource, no function URL/API/event source, no public invoke permission, no network parameter, rules refuse "no address", "both addresses", and `http://` without the override, and accept https, and http with the override; the environment carries `SYTELINE_REQUIRE_HTTPS=true`, `SYTELINE_VERIFY_TLS` defaulting to `true`, and `SYTELINE_LIVE_WRITES=false`.
3. **Transport guard tests**: `pytest tests/unit/test_transport.py` — `health` (normal and dry run) and the notes actions return `INSECURE_TRANSPORT` before any ERP access when the guard is on and the address is `http://`; the override lets them proceed with the `warnings` entry; the guard off leaves the VPC version unchanged.
4. **Deploy script tests**: `pytest tests/unit/test_deploy_script.py` — standalone mode uses the standalone stack and template, refuses network settings, refuses `http://` without the override, requires the approval reference for live writes, honors `--profile`.
5. **Build**: `./deploy.sh --standalone --build`; the built template keeps both rules and has no `VpcConfig`.
6. **Deploy (needs your account and a reachable ERP)**: fill `deploy.env` (the stand-alone file `deploy.standalone.env` was merged into it by feature 005), run `./deploy.sh --standalone --profile <name>`. Confirm in the console that the function shows "no VPC", then invoke the dry-run health check with permitted credentials; expect `load, dns, tcp, wsdl`. Invoke it with credentials lacking `lambda:InvokeFunction`; expect denial.
7. **Coexistence**: with the VPC stack also deployed, deploy and delete the stand-alone stack; the VPC stack's resources and outputs are unchanged (SC-005).
8. **Teardown**: `sam delete --stack-name sytline-connect-standalone --config-env standalone`.

## Attached mode (revision 2026-10-21)

- Offline: `pytest tests/unit/test_template_standalone.py tests/unit/test_secret_store.py tests/unit/test_deploy_script.py -k "attach or secret"` — all three settings give exactly those subnets and security group; any partial set is refused; the VPC-access permission exists only when attached; no API, URL or event source in either mode; an unreachable secret store gives `SECRET_STORE_UNREACHABLE` promptly, and `diagnose` shows the store's address.
- Deploy (needs an existing network, BLOCKED here): fill the three `ATTACH_*` settings; the console shows exactly those subnets and security group; run the dry-run health check with `"diagnose": true`; expect the ERP name to resolve from the plant DNS and a connection to the ERP and to the Secrets Manager address.
