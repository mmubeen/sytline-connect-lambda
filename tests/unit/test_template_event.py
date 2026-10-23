"""The event intake additions to src/template.yaml (specs/006-event-document-result/plan.md, research R4 and R11)."""

import re

from cfn_eval import Evaluator

ARN = "arn:aws-us-gov:states:us-gov-east-1:108375521309:stateMachine:us-ct-idp-stack-PATTERNSTACK-19VN2CRUY3R9A-DocumentProcessingWorkflow"
SUPPLY = dict(WorkerVpcId="vpc-1", WorkerSubnetIds="subnet-a,subnet-b", WorkerSecurityGroupIds="sg-1",
              WorkerRouteTableIds="rtb-1", ApiVpcEndpointId="vpce-1")


def props(template, name):
    return template["Resources"][name]["Properties"]


def test_there_is_no_rule_and_nothing_starts_automatically_without_the_state_machine_arn(template):
    e = Evaluator(template, **SUPPLY)
    assert e.condition("HasIdpEvents") is False
    for name in ("IdpDocumentEventRule", "IdpDocumentEventPermission"):
        assert template["Resources"][name]["Condition"] == "HasIdpEvents"
    assert "EventIntakeFunction" not in template["Resources"]  # one Lambda: the intake is an action of DirectFunction
    assert not template["Resources"]["DirectFunction"].get("Condition")  # it can always be invoked directly
    assert "Events" not in props(template, "DirectFunction")  # no always-on trigger


def test_the_rule_matches_only_succeeded_events_of_that_state_machine(template):
    assert Evaluator(template, IdpStateMachineArn=ARN, **SUPPLY).condition("HasIdpEvents") is True
    pattern = props(template, "IdpDocumentEventRule")["EventPattern"]
    assert pattern == {"source": ["aws.states"], "detail-type": ["Step Functions Execution Status Change"],
                       "detail": {"status": ["SUCCEEDED"], "stateMachineArn": [{"Ref": "IdpStateMachineArn"}]}}
    target = props(template, "IdpDocumentEventRule")["Targets"][0]
    assert target["Arn"] == {"Fn::GetAtt": ["DirectFunction", "Arn"]}
    perm = props(template, "IdpDocumentEventPermission")
    assert perm["FunctionName"] == {"Ref": "DirectFunction"}
    assert perm["Principal"] == "events.amazonaws.com" and perm["SourceArn"] == {"Fn::GetAtt": ["IdpDocumentEventRule", "Arn"]}


def test_the_state_machine_arn_parameter_accepts_an_arn_or_nothing(template):
    p = template["Parameters"]["IdpStateMachineArn"]
    assert p["Default"] == ""
    pattern = re.compile(p["AllowedPattern"])
    assert pattern.match("") and pattern.match(ARN)
    for bad in ("not-an-arn", ARN + " ", "arn:aws:states:us-east-1:1:execution:x:y", ARN.replace("stateMachine", "activity")):
        assert not pattern.match(bad)


def test_the_function_never_retries_and_runs_in_the_plant_network_like_the_worker(template):
    p = props(template, "DirectFunction")
    assert p["Handler"] == "lambda_function.lambda_handler" and p["Timeout"] == 60
    assert p["EventInvokeConfig"]["MaximumRetryAttempts"] == 0
    for mode in (SUPPLY, dict(HostNetworkCidr="10.50.0.0/24", PlantCidr1="10.9.0.0/16", ApiVpcEndpointId="vpce-1")):
        e = Evaluator(template, **mode)
        worker = props(template, "WorkerFunction")["VpcConfig"]
        assert e.resolve(p["VpcConfig"]["SubnetIds"]) == e.resolve(worker["SubnetIds"])
        assert e.resolve(p["VpcConfig"]["SecurityGroupIds"]) == e.resolve(worker["SecurityGroupIds"])


def test_the_function_has_the_same_erp_database_and_idp_settings_as_reserve_plus_the_save_settings(template):
    event_env = props(template, "DirectFunction")["Environment"]["Variables"]
    reserve_env = props(template, "ReserveFunction")["Environment"]["Variables"]
    assert set(reserve_env) <= set(event_env)
    assert {"WORKER_FUNCTION_NAME", "SYTELINE_DEFAULT_WHSE", "SYTELINE_DEFAULT_ORDTYPE"} <= set(event_env)
    assert {k: event_env[k] for k in reserve_env} == reserve_env  # wired identically


def test_the_function_may_use_the_table_the_secret_the_idp_and_start_the_worker(template):
    pols = props(template, "DirectFunction")["Policies"]
    assert {"DynamoDBCrudPolicy": {"TableName": {"Ref": "JobsTable"}}} in pols
    assert {"LambdaInvokePolicy": {"FunctionName": {"Ref": "WorkerFunction"}}} in pols
    secret = [x for x in pols if "Statement" in x][0]["Statement"][0]
    assert secret["Action"] == "secretsmanager:GetSecretValue" and secret["Resource"] == {"Ref": "SytelineSecretArn"}
    assert any("Fn::If" in x for x in pols)  # the IDP read permissions, as for the reserve function


def test_there_is_one_output_for_the_lambda_and_no_separate_intake_function_to_build():
    import os

    root = os.path.join(os.path.dirname(__file__), "..", "..", "src")
    assert "EventIntakeFunction" not in open(os.path.join(root, "Makefile")).read()
    assert "EventIntakeFunction" not in open(os.path.join(root, "template.yaml")).read()


def test_the_outputs_name_the_direct_function_only(template):
    assert "DirectFunctionName" in template["Outputs"] and "EventIntakeFunctionName" not in template["Outputs"]
