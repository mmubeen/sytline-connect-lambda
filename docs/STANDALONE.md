# Stand-alone (no VPC) deployment

A second, independent way to deploy the SyteLine direct-invoke Lambda with **no API gateway, no function URL, no public web address
and no network resources created by the stack.** It runs in one of two modes:

- **No-VPC mode (default):** outside any VPC, with default public networking. Nothing about subnets, VPN or endpoints has to exist.
- **Attached mode:** inside an **existing** VPC, subnets and security group that you supply (all three), so it sees your private DNS
  and routes (for example to an ERP that is only reachable from the plant network). See section 3a.

It contains only the connectivity check (including the credential-free dry run) and the
notes operations (`add_note`, `get_notes`). The order-upload service (private API, worker, sweeper, job table) is **not** part
of it; that stays in the VPC deployment ([DEPLOYMENT.md](DEPLOYMENT.md)).

> **Status:** the template builds and validates locally, but it has not been deployed to a real account. The SyteLine web-service
> names (collections, methods, SOAP operations) are unconfirmed until the read-only discovery task has run; keep live writes off.

## 1. Is this the right version for you?

| | **VPC deployment** (features 001/002) | **Stand-alone** (this) |
|---|---|---|
| Network you must prepare | VPC, private subnets, VPN to the plant (or let the stack create the VPC) | **None** (no-VPC mode), or **an existing VPC, subnets and security group** (attached mode) |
| Where the functions run | inside your VPC | outside any VPC (default), or inside the existing VPC you supply |
| How the ERP is reached | privately over the plant VPN, HTTP port 80 is fine | no-VPC: **over the internet (the ERP must be reachable from the internet)**; attached: over your network's routes |
| ERP connection | HTTP or HTTPS | **HTTPS required** (plain HTTP only with an explicit override) |
| Who can call it | IAM callers through a private API (`execute-api` endpoint); direct invoke | IAM callers by **direct invoke only**; no API, no public web address |
| Outbound address | fixed by your network | no-VPC: **not fixed**, so **ERP source-address allow-listing is not possible**; attached: whatever your network provides |
| Operations | connectivity check, notes, **order upload**, status | connectivity check and notes only |

**Use no-VPC mode when** the SyteLine web service can be reached from the internet: a test or staging ERP, a vendor-hosted ERP, or a plant
that publishes the service through a secure gateway you operate. **Use attached mode when** SyteLine is reachable only through a network you already have (plant VPN, private DNS) and you want the same
lightweight, direct-invoke-only deployment. **Use the VPC deployment** when you also need the order-upload service.

The deployer is responsible for making the ERP reachable and for any gateway or firewall in front of it. In no-VPC mode, an ERP that
restricts access by source address cannot be used.

## 2. How it is protected

- **Callers:** nobody can invoke the function without cloud credentials that have been granted `lambda:InvokeFunction` on it. There is
  no function URL, no API and no resource policy for public principals. Cross-account callers need a resource policy that **you** add.
- **ERP connection: HTTPS by default (in both modes).** The ERP address must be `https://`. A plain `http://` address is refused **at deployment**
  (the stack rules and `deploy.sh`) **and at run time** (the function returns error code `INSECURE_TRANSPORT` before anything is sent
  to the ERP, the dry run included). The run-time check also covers the service address that the ERP's WSDL declares for the SOAP
  calls: a WSDL fetched over `https://` that declares an `http://` endpoint is refused before the password is sent.
- **The override.** `ALLOW_PLAIN_HTTP=true` (stack parameter `AllowPlainHttp`) is a deliberate opt-in. With it on, **the ERP password
  crosses the network unencrypted**. `deploy.sh` warns on every deployment, the stack output `PlainHttpOverride` says `true`, and the
  `health` result carries a warning. Turn it off again as soon as the ERP offers HTTPS. The dry-run health check sends no credentials, so
  it works either way.
- **TLS verification** is on by default (`VerifyTls=true`). Switch it off only for a self-signed test ERP.
- **Credentials** come only from the Secrets Manager secret. There is no password parameter or environment variable. The address must
  not contain credentials (`user:pass@host` is refused). Messages never repeat the address.
- **Live ERP writes** stay off unless you set `LIVE_WRITES=true` **and** an approval reference (the ERP owner's written approval).
- **Public AWS endpoints:** the function reaches Secrets Manager over its public service address (FIPS endpoints by default in
  GovCloud). No private endpoints are involved.

## 3. Deploy

Prerequisites: SAM CLI, AWS CLI with a GovCloud profile, a Secrets Manager secret (JSON with `SYTELINE_PASS`) in the deployment
region, and an ERP address reachable from the internet. See [DEPLOYMENT.md](DEPLOYMENT.md) section 2 for the tools and the secret.

```
cd src
cp deploy.standalone.env.example deploy.standalone.env     # fill in; git-ignored, holds no passwords
./deploy.sh --standalone --profile <govcloud-profile> --validate
./deploy.sh --standalone --profile <govcloud-profile> --build
./deploy.sh --standalone --profile <govcloud-profile>      # build + deploy, interactive change-set review
```

`deploy.sh --standalone` uses its own stack (`sytline-connect-standalone`), template (`src/template-standalone.yaml`), build directory
(`.aws-sam/standalone`) and settings file. It **refuses any network setting** (`WORKER_*`, `HOST_NETWORK_CIDR`, `PLANT_CIDR_*`,
`DYNAMODB_PREFIX_LIST_ID`, `API_VPCE_ID`, `CREATE_API_ENDPOINT`, `CREATE_VPC_ENDPOINTS`) as not applicable; the only network settings it accepts are the three `ATTACH_*` ones.

| Setting (`deploy.standalone.env`) | Meaning |
|---|---|
| `SYTELINE_WSDL` | the ERP web service WSDL; `https://` (an `http://` address needs `ALLOW_PLAIN_HTTP=true`) |
| `SYTELINE_USER`, `SYTELINE_SECRET_ARN`, `SYTELINE_CONFIG` | as in the VPC deployment |
| `ALLOW_PLAIN_HTTP` | `false` (default). `true` = plain HTTP allowed, **credentials unencrypted** |
| `VERIFY_TLS` | `true` (default) |
| `LIVE_WRITES`, `LIVE_APPROVAL_REF` | `false` by default; `true` needs the approval reference |
| `ATTACH_VPC_ID`, `ATTACH_SUBNET_IDS`, `ATTACH_SG_IDS` | optional: attach to an existing network. **All three or none**; a partial set is refused (section 3a) |
| `AWS_PROFILE_NAME` | AWS credentials profile (`--profile` overrides it) |

It coexists with the VPC stack in the same account and region: separate stack, layer and function names, no shared build, no
exports. Deploying or deleting it does not touch the VPC deployment.

## 3a. Attach to an existing VPC, subnets and security group

Fill in all three of `ATTACH_VPC_ID`, `ATTACH_SUBNET_IDS` (comma-separated, no spaces) and `ATTACH_SG_IDS` in
`deploy.standalone.env`, then deploy as usual. The functions then run inside those subnets with that security group. The stack
creates **no** network resource, and attaching adds **no** API gateway, function URL or public address: the only way in is still a
direct invocation by a permitted cloud identity. The function's role gets network-interface permission **only** in this mode.

What your network must provide (the stack creates none of it):
- a **route to the ERP** and, if the ERP name is private, **DNS that resolves it** from those subnets (for a plant name: a Route 53
  Resolver forwarding rule to the plant DNS servers);
- a **private path to Secrets Manager**: an existing Secrets Manager interface endpoint with private DNS in that VPC, or a NAT route.
  A function in a VPC loses default internet access; without this path it cannot read the ERP password;
- a security group that allows **outbound HTTPS to Secrets Manager** and **outbound access to the ERP's port**.

If the path to Secrets Manager is missing, the health check fails promptly (it does not hang) with error code
`SECRET_STORE_UNREACHABLE`; add `"diagnose": true` to see whether the Secrets Manager address resolves and connects.
The HTTPS rule is the same as in no-VPC mode. Switching an existing deployment between the two modes updates the functions'
network settings in place (detaching can take several minutes to release the network interfaces).

## 4. Allow a caller to invoke it

Grant the calling principal permission, for example attach this to its IAM role:

```
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"lambda:InvokeFunction",
  "Resource":"<DirectFunctionArn from the stack outputs>"}]}
```

## 5. Verify

```
aws cloudformation describe-stacks --region us-gov-east-1 --stack-name sytline-connect-standalone \
    --query 'Stacks[0].Outputs' --output table
aws lambda invoke --region us-gov-east-1 --function-name <DirectFunctionName> \
    --payload '{"action":"health","dry_run":true}' --cli-binary-format raw-in-base64-out out.json && cat out.json
```

- Dry run: expect stages `load`, `dns`, `tcp`, `wsdl`. Full check (`{"action":"health"}`): `dns`, `tcp`, `wsdl`, `token`.
- If a stage fails, add `"diagnose": true` to the payload for a `diagnostics` section with plain-language `findings` (for example: "general name resolution works but the ERP name did not resolve"). It contacts only the ERP host and the regional Secrets Manager endpoint.
- Console: the function's configuration shows **no VPC** (no-VPC mode) or exactly your subnets and security group (attached mode); the stack output `NetworkMode` says which.
- Without `lambda:InvokeFunction`, the call is denied.
- An `http://` address without the override returns `{"ok": false, "errors": [{"code": "INSECURE_TRANSPORT", ...}]}` and sends nothing to the ERP.

## 6. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Deployment refused: "Give exactly one of SytelineWsdlHttps or SytelineWsdlHttp" | use `deploy.sh --standalone`, or pass exactly one address |
| Deployment refused: "An http:// ERP address needs AllowPlainHttp=true" | use an `https://` address, or set `ALLOW_PLAIN_HTTP=true` knowing the consequence |
| `deploy.sh`: "not applicable to the stand-alone version" | a network setting is in `deploy.standalone.env`; remove it |
| `INSECURE_TRANSPORT` at run time | the ERP address (or the service address in its WSDL) is `http://`; fix the ERP or set the override |
| `health` fails at `dns` / `tcp` | no-VPC mode: the ERP name is not publicly resolvable or the ERP is not reachable from the internet, or a firewall blocks the function. Private plant name: use attached mode |
| `SECRET_STORE_UNREACHABLE` | attached mode without a path to Secrets Manager (add an endpoint with private DNS or a NAT route; allow HTTPS from the security group), or a wrong/denied secret ARN |
| Deployment refused: "Supply all three of AttachVpcId ..." | give all three `ATTACH_*` settings or none |
| `health` fails at `token` | wrong user, or `SYTELINE_PASS` missing from the secret |
| TLS errors | certificate not trusted: fix the ERP certificate, or `VERIFY_TLS=false` only for a self-signed test ERP |

## 7. Remove

```
sam delete --stack-name sytline-connect-standalone --config-env standalone --region us-gov-east-1
```

The Secrets Manager secret is not part of the stack and is not deleted. The VPC deployment is unaffected.
