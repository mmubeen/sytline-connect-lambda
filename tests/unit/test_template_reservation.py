"""Structure of the job-reservation additions to scr/template.yaml (specs/004-job-number-workflow/plan.md)."""

from cfn_eval import Evaluator

SUPPLY = dict(WorkerVpcId="vpc-1", WorkerSubnetIds="subnet-a,subnet-b", WorkerSecurityGroupIds="sg-1",
              WorkerRouteTableIds="rtb-1", ApiVpcEndpointId="vpce-1")
CREATE = dict(HostNetworkCidr="10.50.0.0/24", PlantCidr1="10.9.0.0/16", ApiVpcEndpointId="vpce-1")


def props(template, name):
    return template["Resources"][name]["Properties"]


def events(template, name):
    return props(template, name)["Events"]


def api_routes(template):
    """(path, method) of every event on OrderApi."""
    out = set()
    for r in template["Resources"].values():
        if r["Type"] != "AWS::Serverless::Function":
            continue
        for ev in (r["Properties"].get("Events") or {}).values():
            p = ev["Properties"]
            if ev["Type"] == "Api" and p.get("RestApiId") == {"Ref": "OrderApi"}:
                out.add((p["Path"], p["Method"]))
    return out


def policy_names(template, name):
    out = []
    for pol in props(template, name).get("Policies", []):
        out.append(next(iter(pol)) if "Statement" not in pol else "Statement")
    return out


def test_reserve_function_is_in_the_plant_network_like_the_worker(template):
    for mode in (CREATE, SUPPLY):
        e = Evaluator(template, **mode)
        for fn in ("ReserveFunction", "WorkerFunction"):
            vc = props(template, fn)["VpcConfig"]
            assert e.resolve(vc["SubnetIds"]) == e.resolve(props(template, "WorkerFunction")["VpcConfig"]["SubnetIds"])
            assert e.resolve(vc["SecurityGroupIds"]) == e.resolve(
                props(template, "WorkerFunction")["VpcConfig"]["SecurityGroupIds"])


def test_reserve_function_fits_the_api_limit_and_has_the_erp_settings_and_permissions(template):
    p = props(template, "ReserveFunction")
    assert p["Handler"] == "handlers.reserve.handler" and p["Timeout"] < 29
    env = p["Environment"]["Variables"]
    assert {"SYTELINE_WSDL", "SYTELINE_USER", "SYTELINE_CONFIG", "SYTELINE_SECRET_ARN", "SYTELINE_LIVE_WRITES"} <= set(env)
    assert "DynamoDBCrudPolicy" in policy_names(template, "ReserveFunction")
    secret = [x for x in p["Policies"] if "Statement" in x][0]["Statement"][0]
    assert secret["Action"] == "secretsmanager:GetSecretValue" and secret["Resource"] == {"Ref": "SytelineSecretArn"}


def test_reserve_route_is_on_the_private_api_with_iam_auth(template):
    assert ("/job-reservations", "post") in api_routes(template)
    assert template["Resources"]["OrderApi"]["Properties"]["Auth"]["DefaultAuthorizer"] == "AWS_IAM"
    assert template["Resources"]["OrderApi"]["Properties"]["EndpointConfiguration"]["Type"] == "PRIVATE"


def test_existing_routes_are_unchanged(template):
    assert {("/confirmed-orders", "post"), ("/upload-jobs/{id}", "get")} <= api_routes(template)


def test_no_new_network_resource_and_worker_stays_serialized(template):
    assert not [n for n, r in template["Resources"].items()
                if r["Type"].startswith("AWS::EC2::") and "eserv" in n]
    assert props(template, "WorkerFunction")["ReservedConcurrentExecutions"] == 1
    assert props(template, "WorkerFunction")["EventInvokeConfig"]["MaximumRetryAttempts"] == 0


def test_save_intake_is_like_the_order_intake_and_the_worker_serves_both(template):
    assert ("/job-reservations/{id}/save", "post") in api_routes(template)
    p, upload = props(template, "SaveIntakeFunction"), props(template, "IntakeFunction")
    assert p["Handler"] == "handlers.save_intake.handler" and p["Timeout"] == upload["Timeout"]
    assert p["Environment"] == upload["Environment"] and p["Policies"] == upload["Policies"]
    assert "VpcConfig" not in p  # records and hands over only; the worker (in the VPC) writes to the ERP


def test_reservation_status_is_read_only_like_the_upload_status(template):
    assert ("/job-reservations/{id}", "get") in api_routes(template)
    p, upload = props(template, "ReservationStatusFunction"), props(template, "StatusFunction")
    assert p["Handler"] == "handlers.reservation_status.handler" and p["Policies"] == upload["Policies"]
    assert policy_names(template, "ReservationStatusFunction") == ["DynamoDBReadPolicy"]
    assert "VpcConfig" not in p and p["Timeout"] == upload["Timeout"]


def test_all_four_new_routes_exist_and_nothing_else_was_added_to_the_api(template):
    assert api_routes(template) == {("/confirmed-orders", "post"), ("/upload-jobs/{id}", "get"),
                                    ("/job-reservations", "post"), ("/job-reservations/{id}/save", "post"),
                                    ("/job-reservations/{id}", "get")}
