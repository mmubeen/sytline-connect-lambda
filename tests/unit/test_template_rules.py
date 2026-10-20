"""The template's Rules refuse bad network input before anything is created (FR-002, FR-004, SC-003)."""

import pytest

from cfn_eval import Evaluator

CREATE = dict(HostNetworkCidr="10.50.0.0/24", PlantCidr1="10.9.0.0/16", ApiVpcEndpointId="vpce-1")
SUPPLY = dict(WorkerVpcId="vpc-1", WorkerSubnetIds="subnet-a,subnet-b", WorkerSecurityGroupIds="sg-1",
              WorkerRouteTableIds="rtb-1", ApiVpcEndpointId="vpce-1")


def ev(template, **p):
    return Evaluator(template, **p)


def test_create_mode_is_accepted(template):
    assert ev(template, **CREATE).violations() == []


def test_create_mode_with_created_api_endpoint_is_accepted(template):
    p = dict(CREATE, ApiVpcEndpointId="", CreateApiEndpoint="true")
    assert ev(template, **p).violations() == []


def test_supply_mode_is_accepted(template):
    assert ev(template, **SUPPLY).violations() == []


@pytest.mark.parametrize("missing", ["WorkerVpcId", "WorkerSubnetIds", "WorkerSecurityGroupIds", "WorkerRouteTableIds"])
def test_each_partial_set_is_refused(template, missing):
    p = dict(SUPPLY, **{missing: ""})
    msgs = ev(template, **p).violations()
    assert any("Supply all four" in m for m in msgs), msgs


@pytest.mark.parametrize("given", ["WorkerVpcId", "WorkerSubnetIds", "WorkerSecurityGroupIds", "WorkerRouteTableIds"])
def test_a_single_identifier_alone_is_refused(template, given):
    p = dict(CREATE, **{given: SUPPLY[given]})
    assert any("Supply all four" in m for m in ev(template, **p).violations())


@pytest.mark.parametrize("drop", ["HostNetworkCidr", "PlantCidr1"])
def test_create_mode_needs_both_ranges(template, drop):
    p = dict(CREATE, **{drop: ""})
    assert any("required when the host network is created" in m for m in ev(template, **p).violations())


def test_api_endpoint_is_required_unless_the_option_creates_one(template):
    assert any("Provide ApiVpcEndpointId" in m for m in ev(template, **dict(CREATE, ApiVpcEndpointId="")).violations())
    p = dict(SUPPLY, ApiVpcEndpointId="")
    assert any("Provide ApiVpcEndpointId" in m for m in ev(template, **p).violations())


def test_option_with_a_supplied_network_and_no_endpoint_is_refused(template):
    p = dict(SUPPLY, ApiVpcEndpointId="", CreateApiEndpoint="true")
    assert any("Provide ApiVpcEndpointId" in m for m in ev(template, **p).violations())  # option has no effect there


def test_option_with_an_existing_endpoint_id_is_contradictory(template):
    for base in (CREATE, SUPPLY):
        p = dict(base, CreateApiEndpoint="true", ApiVpcEndpointId="vpce-9")
        assert any("cannot be combined" in m for m in ev(template, **p).violations())


@pytest.mark.parametrize("cidr,ok", [("10.0.0.0/16", True), ("10.0.0.0/25", True), ("192.168.1.0/24", True),
                                     ("", True), ("10.0.0.0/8", False), ("10.0.0.0/26", False), ("ten", False),
                                     ("10.0.0.0", False)])
def test_host_network_cidr_pattern(template, cidr, ok):
    errs = ev(template, **dict(CREATE, HostNetworkCidr=cidr)).parameter_errors()
    assert (errs == []) is ok, errs


@pytest.mark.parametrize("cidr,ok", [("10.9.0.0/16", True), ("", True), ("10.9.0.0", False), ("x/16", False)])
def test_plant_cidr_pattern(template, cidr, ok):
    assert (ev(template, **dict(CREATE, PlantCidr2=cidr)).parameter_errors() == []) is ok


def test_prefix_list_and_option_values(template):
    assert ev(template, **dict(CREATE, DynamoDbPrefixListId="pl-63a5400a")).parameter_errors() == []
    assert ev(template, **dict(CREATE, DynamoDbPrefixListId="nope")).parameter_errors()
    assert ev(template, **dict(CREATE, CreateApiEndpoint="maybe")).parameter_errors()
