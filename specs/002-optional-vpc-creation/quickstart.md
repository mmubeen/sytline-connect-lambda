# Quickstart: validating optional host network creation

Nothing here deploys to AWS unless a step says so. Steps 1-3 run offline.

1. **Static checks** (`cd src`): `cfn-lint template.yaml` and `sam validate --lint --region us-gov-east-1` must pass for the template with its default (empty) network parameters.
2. **Structure and rules tests**: `pytest tests/unit/test_template_network.py tests/unit/test_template_rules.py`.
   Expect: no internet gateway, NAT gateway, elastic IP or default route is defined; every network resource is conditioned on `CreateNetwork`; the Create-mode outputs exist; the rules accept "none supplied with cidrs", "all four supplied", and refuse each partial case, a missing CIDR, a missing API endpoint, and the contradictory endpoint option.
3. **Deploy script**: `pytest tests/unit/test_deploy_script.py` (stubbed `sam`/`aws`). Expect the same refusals as the rules, and a refusal to switch mode against an existing stack without `--allow-network-change`.
4. **Create mode (deploys; needs your account)**: in `deploy.env` leave the four network settings empty and set `HOST_NETWORK_CIDR`, `PLANT_CIDR_1`, `SYTELINE_*`, `CREATE_API_ENDPOINT` as needed; run `./deploy.sh`. Verify with `aws ec2 describe-route-tables` that the created table has only the local route, and that the outputs list the VPC, subnets, route table, security group and ranges.
5. **Handover**: give the outputs to the network team; once they have attached the gateway, added the plant route to `HostRouteTableId` and fixed plant DNS, run the health check (`docs/DEPLOYMENT.md` section 5). Before that, it must fail at `dns` or `tcp` with the usual stage message.
6. **Supply mode (deploys)**: with all four identifiers set, confirm the change set contains no network resources, and that an existing 001 stack updates with no network change (SC-002, SC-006).
7. **Teardown**: `sam delete`; if a gateway was attached to the VPC, expect the deletion to stop at the VPC until it is detached.
