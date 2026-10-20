"""Structure of the optional host network in scr/template.yaml (plan.md, data-model.md)."""

import pytest

from cfn_eval import Evaluator

CREATE = dict(HostNetworkCidr="10.50.0.0/24", PlantCidr1="10.9.0.0/16", ApiVpcEndpointId="vpce-1")
SUPPLY = dict(WorkerVpcId="vpc-1", WorkerSubnetIds="subnet-a,subnet-b", WorkerSecurityGroupIds="sg-1",
              WorkerRouteTableIds="rtb-1", ApiVpcEndpointId="vpce-1")
CREATED = {"HostVpc", "HostSubnetA", "HostSubnetB", "HostRouteTable", "FunctionSecurityGroup", "ApiEndpoint"}
NETWORK_RESOURCES = {"HostVpc", "HostSubnetA", "HostSubnetB", "HostRouteTable", "RtAssocA", "RtAssocB",
                     "FunctionSecurityGroup", "EndpointSecurityGroup", "EgressPlant2", "EgressPlant3",
                     "EgressHostRange", "EgressDdbPrefix", "EgressDdbAny", "ApiEndpoint"}
FORBIDDEN_TYPES = ("AWS::EC2::InternetGateway", "AWS::EC2::NatGateway", "AWS::EC2::EIP", "AWS::EC2::VPNGateway",
                   "AWS::EC2::VPNConnection", "AWS::EC2::VPCGatewayAttachment", "AWS::EC2::CustomerGateway",
                   "AWS::EC2::VPNGatewayRoutePropagation", "AWS::EC2::TransitGateway")


def ev(template, **p):
    return Evaluator(template, **p)


def res(template, name):
    return template["Resources"][name]


def egress_rules(template, e):
    """(protocol-port, destination) tuples of the FunctionSecurityGroup egress, inline and separate."""
    out = []
    sg = res(template, "FunctionSecurityGroup")["Properties"]
    for r in sg.get("SecurityGroupEgress", []):
        out.append((r["FromPort"], e.resolve(r.get("CidrIp") or r.get("DestinationPrefixListId"))))
    for n in e.active():
        r = res(template, n)
        if r["Type"] == "AWS::EC2::SecurityGroupEgress":
            p = r["Properties"]
            out.append((p["FromPort"], e.resolve(p.get("CidrIp") or p.get("DestinationPrefixListId"))))
    return sorted(out, key=str)


# ---- US1: Create mode ------------------------------------------------------------------
def test_create_mode_builds_the_network(template):
    e = ev(template, **CREATE)
    act = e.active()
    assert {"HostVpc", "HostSubnetA", "HostSubnetB", "HostRouteTable", "RtAssocA", "RtAssocB",
            "FunctionSecurityGroup", "EndpointSecurityGroup", "DynamoDbEndpoint", "SecretsManagerEndpoint"} <= act
    vpc = res(template, "HostVpc")["Properties"]
    assert e.resolve(vpc["CidrBlock"]) == "10.50.0.0/24" and vpc["EnableDnsSupport"] and vpc["EnableDnsHostnames"]
    a, b = res(template, "HostSubnetA")["Properties"], res(template, "HostSubnetB")["Properties"]
    assert e.resolve(a["CidrBlock"]) == "10.50.0.0/26" and e.resolve(b["CidrBlock"]) == "10.50.0.64/26"
    assert e.resolve(a["AvailabilityZone"]) == "az-0" and e.resolve(b["AvailabilityZone"]) == "az-1"
    assert a["MapPublicIpOnLaunch"] is False and b["MapPublicIpOnLaunch"] is False


def test_one_route_table_with_two_associations_and_no_route(template):
    types = [r["Type"] for r in template["Resources"].values()]
    assert types.count("AWS::EC2::RouteTable") == 1
    assert types.count("AWS::EC2::SubnetRouteTableAssociation") == 2
    assert "AWS::EC2::Route" not in types


def test_function_security_group_has_no_ingress_and_the_expected_egress(template):
    sg = res(template, "FunctionSecurityGroup")["Properties"]
    assert "SecurityGroupIngress" not in sg
    assert not [r for r in template["Resources"].values() if r["Type"] == "AWS::EC2::SecurityGroupIngress"]
    e = ev(template, **CREATE)
    assert egress_rules(template, e) == sorted([(80, "10.9.0.0/16"), (443, "10.50.0.0/24"), (443, "0.0.0.0/0")], key=str)
    e = ev(template, **dict(CREATE, PlantCidr2="10.8.0.0/16", PlantCidr3="10.7.0.0/16"))
    assert (80, "10.8.0.0/16") in egress_rules(template, e) and (80, "10.7.0.0/16") in egress_rules(template, e)
    e = ev(template, **dict(CREATE, DynamoDbPrefixListId="pl-63a5400a"))
    rules = egress_rules(template, e)
    assert (443, "pl-63a5400a") in rules and (443, "0.0.0.0/0") not in rules


def test_endpoint_security_group_allows_https_from_the_host_range_only(template):
    sg = res(template, "EndpointSecurityGroup")["Properties"]
    ing = sg["SecurityGroupIngress"]
    assert len(ing) == 1 and ing[0]["FromPort"] == 443 and ing[0]["ToPort"] == 443
    assert ev(template, **CREATE).resolve(ing[0]["CidrIp"]) == "10.50.0.0/24"


def test_endpoints_use_the_created_network(template):
    e = ev(template, **CREATE)
    ddb = res(template, "DynamoDbEndpoint")["Properties"]
    assert e.resolve(ddb["VpcId"]) == "@HostVpc" and e.resolve(ddb["RouteTableIds"]) == ["@HostRouteTable"]
    sm = res(template, "SecretsManagerEndpoint")["Properties"]
    assert e.resolve(sm["VpcId"]) == "@HostVpc"
    assert e.resolve(sm["SubnetIds"]) == ["@HostSubnetA", "@HostSubnetB"]
    assert e.resolve(sm["SecurityGroupIds"]) == ["@EndpointSecurityGroup"]


@pytest.mark.parametrize("fn", ["WorkerFunction", "DirectFunction"])
def test_functions_attach_to_the_created_network(template, fn):
    vc = res(template, fn)["Properties"]["VpcConfig"]
    e = ev(template, **CREATE)
    assert e.resolve(vc["SubnetIds"]) == ["@HostSubnetA", "@HostSubnetB"]
    assert e.resolve(vc["SecurityGroupIds"]) == ["@FunctionSecurityGroup"]


# ---- US1: invariants (FR-003, FR-008, SC-004) -------------------------------------------
def test_no_gateway_nat_eip_vpn_or_default_route_anywhere(template):
    for name, r in template["Resources"].items():
        assert not r["Type"].startswith(FORBIDDEN_TYPES), f"{name} is a forbidden {r['Type']}"
        if r["Type"] == "AWS::EC2::Route":
            assert r["Properties"].get("DestinationCidrBlock") != "0.0.0.0/0"


def test_every_resource_that_references_a_created_resource_is_conditioned_on_create_network(template):
    """Research prototype finding: an optional rule must not reference a Create-mode resource in Supply mode."""
    for mode in (SUPPLY, CREATE, dict(CREATE, PlantCidr2="10.8.0.0/16", PlantCidr3="10.7.0.0/16",
                                      DynamoDbPrefixListId="pl-1", CreateApiEndpoint="true", ApiVpcEndpointId="")):
        e = ev(template, **mode)
        for name in e.active():
            r = res(template, name)
            refs = e.live_refs({k: v for k, v in r.items() if k != "Condition"})
            missing = {x for x in refs if x in template["Resources"] and x not in e.active()}
            assert not missing, f"{name} is active but references inactive {missing}"


def test_create_mode_resources_all_carry_a_condition_that_includes_create_network(template):
    for name in NETWORK_RESOURCES & set(template["Resources"]):
        cond = res(template, name).get("Condition")
        assert cond, f"{name} has no condition"
        assert _includes_create_network(template, cond), f"{name}: condition {cond} does not include CreateNetwork"


def _includes_create_network(template, cond):
    if cond == "CreateNetwork":
        return True
    c = template["Conditions"][cond]
    return _walk(template, c)


def _walk(template, e):
    if isinstance(e, dict):
        (k, v), = e.items()
        if k == "Condition":
            return _includes_create_network(template, v)
        if k == "Fn::And":
            return any(_walk(template, x) for x in v)
    return False


def test_subnet_cidr_bits_and_no_public_ip(template):
    for n in ("HostSubnetA", "HostSubnetB"):
        cb = res(template, n)["Properties"]["CidrBlock"]["Fn::Select"][1]["Fn::Cidr"]
        assert cb[1] == 2 and cb[2] == 6


# ---- US1: optional API-caller endpoint (FR-009) -----------------------------------------
def _api(template, e):
    api = res(template, "OrderApi")["Properties"]
    return (e.resolve(api["EndpointConfiguration"]["VPCEndpointIds"]),
            e.resolve(api["Auth"]["ResourcePolicy"]["IntrinsicVpcWhitelist"]))


def test_option_off_uses_the_supplied_endpoint(template):
    e = ev(template, **CREATE)
    assert "ApiEndpoint" not in e.active()
    assert _api(template, e) == (["vpce-1"], ["vpce-1"])


def test_option_on_in_create_mode_creates_the_endpoint_and_uses_it(template):
    e = ev(template, **dict(CREATE, ApiVpcEndpointId="", CreateApiEndpoint="true"))
    assert "ApiEndpoint" in e.active()
    p = res(template, "ApiEndpoint")["Properties"]
    assert p["VpcEndpointType"] == "Interface" and p["PrivateDnsEnabled"] is True
    assert e.resolve(p["SubnetIds"]) == ["@HostSubnetA", "@HostSubnetB"]
    assert e.resolve(p["SecurityGroupIds"]) == ["@EndpointSecurityGroup"]
    assert "execute-api" in p["ServiceName"]["Fn::Sub"]
    assert _api(template, e) == (["@ApiEndpoint"], ["@ApiEndpoint"])


def test_option_on_with_a_supplied_network_creates_nothing(template):
    e = ev(template, **dict(SUPPLY, CreateApiEndpoint="true"))
    assert "ApiEndpoint" not in e.active()  # (and the rules refuse the combination, see test_template_rules)


# ---- US2: Supply mode is identical to feature 001 ---------------------------------------
def test_supply_mode_creates_no_network_resources(template):
    e = ev(template, **SUPPLY)
    assert not (e.active() & NETWORK_RESOURCES), e.active() & NETWORK_RESOURCES


def test_stray_create_mode_settings_with_a_full_set_do_nothing(template):
    e = ev(template, **dict(SUPPLY, HostNetworkCidr="10.50.0.0/24", PlantCidr1="10.9.0.0/16",
                            PlantCidr2="10.8.0.0/16", PlantCidr3="10.7.0.0/16", DynamoDbPrefixListId="pl-1"))
    assert not (e.active() & NETWORK_RESOURCES)
    assert e.violations() == []


def test_supply_mode_endpoints_follow_create_vpc_endpoints(template):
    on = ev(template, **dict(SUPPLY, CreateVpcEndpoints="true"))
    off = ev(template, **dict(SUPPLY, CreateVpcEndpoints="false"))
    assert {"DynamoDbEndpoint", "SecretsManagerEndpoint"} <= on.active()
    assert not ({"DynamoDbEndpoint", "SecretsManagerEndpoint"} & off.active())
    ddb = res(template, "DynamoDbEndpoint")["Properties"]
    assert on.resolve(ddb["VpcId"]) == "vpc-1" and on.resolve(ddb["RouteTableIds"]) == ["rtb-1"]
    sm = res(template, "SecretsManagerEndpoint")["Properties"]
    assert on.resolve(sm["SubnetIds"]) == ["subnet-a", "subnet-b"] and on.resolve(sm["SecurityGroupIds"]) == ["sg-1"]


def test_create_mode_ignores_create_vpc_endpoints_false(template):
    e = ev(template, **dict(CREATE, CreateVpcEndpoints="false"))
    assert {"DynamoDbEndpoint", "SecretsManagerEndpoint"} <= e.active()  # the worker needs them in the new network


@pytest.mark.parametrize("fn", ["WorkerFunction", "DirectFunction"])
def test_supply_mode_function_network_settings_are_exactly_feature_001s(template, fn):
    vc = res(template, fn)["Properties"]["VpcConfig"]
    e = ev(template, **SUPPLY)
    assert e.resolve(vc["SubnetIds"]) == ["subnet-a", "subnet-b"]
    assert e.resolve(vc["SecurityGroupIds"]) == ["sg-1"]
    assert _api(template, e) == (["vpce-1"], ["vpce-1"])


def test_live_writes_and_other_001_settings_are_untouched(template):
    p = template["Parameters"]
    assert p["LiveWrites"]["Default"] == "false" and p["LiveWrites"]["AllowedValues"] == ["false", "true"]
    env = res(template, "WorkerFunction")["Properties"]["Environment"]["Variables"]
    assert env["SYTELINE_LIVE_WRITES"] == {"Ref": "LiveWrites"}


# ---- US3: handover outputs --------------------------------------------------------------
HANDOVER = ["HostVpcId", "HostSubnetIds", "HostRouteTableId", "FunctionSecurityGroupId", "HostNetworkCidr",
            "PlantCidrs"]


def _emitted(template, e):
    return {n for n, o in template["Outputs"].items() if "Condition" not in o or e.condition(o["Condition"])}


def test_create_mode_emits_the_handover_outputs(template):
    e = ev(template, **CREATE)
    out = _emitted(template, e)
    assert set(HANDOVER) <= out and "ApiEndpointId" not in out
    assert {"ApiId", "WorkerFunctionName", "DirectFunctionName", "JobsTableName"} <= out
    outs = template["Outputs"]
    assert e.resolve(outs["HostNetworkCidr"]["Value"]) == "10.50.0.0/24"
    assert e.resolve(outs["HostSubnetIds"]["Value"]) == "@HostSubnetA,@HostSubnetB"


def test_api_endpoint_output_only_when_created(template):
    e = ev(template, **dict(CREATE, ApiVpcEndpointId="", CreateApiEndpoint="true"))
    assert "ApiEndpointId" in _emitted(template, e)


def test_supply_mode_emits_no_handover_outputs_and_keeps_the_existing_ones(template):
    out = _emitted(template, ev(template, **SUPPLY))
    assert not (set(HANDOVER) | {"ApiEndpointId"}) & out
    assert {"ApiId", "WorkerFunctionName", "DirectFunctionName", "JobsTableName"} <= out


def test_plant_cidrs_output_lists_the_given_ranges(template):
    e = ev(template, **dict(CREATE, PlantCidr2="10.8.0.0/16"))
    assert e.resolve(template["Outputs"]["PlantCidrs"]["Value"]) == "10.9.0.0/16,10.8.0.0/16"
