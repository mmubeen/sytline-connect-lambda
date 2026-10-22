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


def test_save_intake_reads_idp_so_it_is_in_the_plant_network_and_hands_the_save_to_the_worker(template):
    assert ("/job-reservations/{id}/save", "post") in api_routes(template)
    p, upload = props(template, "SaveIntakeFunction"), props(template, "IntakeFunction")
    assert p["Handler"] == "handlers.save_intake.handler" and p["Timeout"] == upload["Timeout"]
    assert p["Environment"]["Variables"]["WORKER_FUNCTION_NAME"] == {"Ref": "WorkerFunction"}
    assert {"DynamoDBCrudPolicy", "LambdaInvokePolicy"} <= set(policy_names(template, "SaveIntakeFunction"))
    for mode in (CREATE, SUPPLY):  # it now reads the IDP document, so it needs the same network as the worker
        e = Evaluator(template, **mode)
        assert e.resolve(p["VpcConfig"]) == e.resolve(props(template, "WorkerFunction")["VpcConfig"])


IDP_FUNCTIONS = ("ReserveFunction", "SaveIntakeFunction", "WorkerFunction")


def test_the_functions_that_read_idp_get_its_settings_and_read_only_permissions(template):
    for fn in IDP_FUNCTIONS:
        env = props(template, fn)["Environment"]["Variables"]
        assert env["IDP_STACK_NAME"] == {"Ref": "IdpStackName"} and env["IDP_REGION"] == {"Ref": "IdpRegion"}
        cond = [x for x in props(template, fn)["Policies"] if "Fn::If" in x]
        assert len(cond) == 1 and cond[0]["Fn::If"][0] == "HasIdp" and cond[0]["Fn::If"][2] == {"Ref": "AWS::NoValue"}
        actions = {a for st in cond[0]["Fn::If"][1]["Statement"] for a in
                   (st["Action"] if isinstance(st["Action"], list) else [st["Action"]])}
        assert actions == {"cloudformation:DescribeStacks", "cloudformation:DescribeStackResource",
                           "cloudformation:DescribeStackResources", "cloudformation:ListStackResources",
                           "s3:GetObject", "dynamodb:GetItem", "dynamodb:Query", "dynamodb:BatchGetItem"}  # read only
    assert "IDP_STACK_NAME" not in props(template, "ReservationStatusFunction").get("Environment", {}).get(
        "Variables", {})  # the status view never reads IDP


def test_idp_parameters_are_optional_and_all_or_nothing(template):
    for name in ("IdpStackName", "IdpRegion", "IdpOutputBucketName", "IdpTrackingTableName"):
        assert template["Parameters"][name]["Default"] == ""
    rule = template["Rules"]["IdpAllOrNothing"]
    assert "Supply all three" in rule["Assertions"][0]["AssertDescription"]
    e = Evaluator(template, **SUPPLY)
    assert e.condition("HasIdp") is False
    assert Evaluator(template, **dict(SUPPLY, IdpStackName="idp")).condition("HasIdp") is True


def test_every_function_has_a_makefile_build_target():
    """`sam build` fails for a function built with the makefile method when the Makefile has no target for it."""
    import os
    import re

    from cfn_template import load_template

    template = load_template()
    makefile = open(os.path.join(os.path.dirname(__file__), "..", "..", "scr", "Makefile")).read()
    listed = set(re.search(r"^FUNCTIONS\s*=((?:.*\\\n)*.*)$", makefile, re.M).group(1).replace("\\", " ").split())
    functions = {n for n, r in template["Resources"].items() if r["Type"] == "AWS::Serverless::Function"
                 and (r.get("Metadata") or {}).get("BuildMethod") == "makefile"}
    assert functions <= listed, f"no Makefile target for {sorted(functions - listed)}"


def test_the_idp_sdk_is_packaged_from_a_checkout_never_installed_by_name():
    import os

    scr = os.path.join(os.path.dirname(__file__), "..", "..", "scr")
    makefile = open(os.path.join(scr, "Makefile")).read()
    assert "IDP_REPO" in makefile and "idp_sdk" in makefile and "idp_common" in makefile
    for f in ("requirements.txt", "requirements-idp.txt"):  # the squatted PyPI name must never be requested
        lines = [l.split("#")[0].strip().lower() for l in open(os.path.join(scr, f))]
        assert not [l for l in lines if l.replace("_", "-").startswith(("idp-common", "idp-sdk"))]


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
