"""The IDP's side of the world (specs/008 R7): a CloudFormation stack in Moto with the outputs and resources the IDP SDK
reads, the section result objects, and the stand-in for the stack's LookupFunction.

Shapes follow the SDK source (idp_sdk/_core/stack_info.py, document_processor.py, assessment_analyzer.py,
progress_monitor.py); tests/unit/mock/test_idp_seed.py reads every scenario back through the real SDK to keep them true.
"""

import json

LOOKUP_SUFFIX = "ABC123XYZ"


class IdpSeeder:
    def __init__(self, rendered):
        p = rendered["parameters"]
        self.stack = p["IdpStackName"]
        self.bucket = p["IdpOutputBucketName"]
        self.table = p["IdpTrackingTableName"]
        self.lookup_name = f"{self.stack}-LookupFunction-{LOOKUP_SUFFIX}"
        self.state_machine = p["IdpStateMachineArn"]
        self.docs = {}

    def template(self):
        return json.dumps({
            "AWSTemplateFormatVersion": "2010-09-09",
            "Resources": {
                "InputBucket": {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": f"{self.stack}-input"}},
                "OutputBucket": {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": self.bucket}},
                "ConfigurationBucket": {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": f"{self.stack}-config"}},
                "TrackingTable": {"Type": "AWS::DynamoDB::Table", "Properties": {
                    "TableName": self.table, "BillingMode": "PAY_PER_REQUEST",
                    "AttributeDefinitions": [{"AttributeName": "PK", "AttributeType": "S"}],
                    "KeySchema": [{"AttributeName": "PK", "KeyType": "HASH"}]}},
                "DocumentQueue": {"Type": "AWS::SQS::Queue", "Properties": {"QueueName": f"{self.stack}-queue"}},
            },
            "Outputs": {
                "S3InputBucketName": {"Value": f"{self.stack}-input"},
                "S3OutputBucketName": {"Value": self.bucket},
                "S3ConfigurationBucketName": {"Value": f"{self.stack}-config"},
                "LambdaLookupFunctionName": {"Value": self.lookup_name},
                "StateMachineArn": {"Value": self.state_machine},
            }})

    def base(self, world):
        """The stack and its resources exist (called after every reset)."""
        self.docs = {}
        world.client("cloudformation").create_stack(StackName=self.stack, TemplateBody=self.template())

    def document(self, world, scenario):
        """Put the scenario's document in the output bucket and the lookup table (the scenario's idp.json)."""
        d = scenario["idp"]
        self.docs[d["document_id"]] = {"status": d["status"]}
        if "kms_key" in d:
            world.kms[f"arn:{world.r['function_arn'].split(':')[1]}:s3:::{self.bucket}/"] = d["kms_key"]
        if not d.get("present", True):
            return
        explain = [{name: info for name, info in d.get("confidence", {}).items()}]
        result = {"inference_result": d["fields"], "document_class": {"type": d["class"], "confidence": 0.99},
                  "split_document": {"page_indices": list(range(d.get("pages", 1)))}, "explainability_info": explain}
        world.client("s3").put_object(Bucket=self.bucket, Key=f"{d['document_id']}/sections/1/result.json",
                                      Body=json.dumps(result).encode())

    def hide(self, world):
        """`idp.not_found`: the IDP has no record of the loaded scenario's document (restored by `show`)."""
        self._hidden = {}
        for doc_id, info in list(self.docs.items()):
            key = f"{doc_id}/sections/1/result.json"
            try:
                body = world.client("s3").get_object(Bucket=self.bucket, Key=key)["Body"].read()
                world.client("s3").delete_object(Bucket=self.bucket, Key=key)
                self._hidden[doc_id] = (key, body)
            except Exception:  # the document was never put there (a scenario without one)
                pass
            self._hidden.setdefault("_status_" + doc_id, info["status"])
            info["status"] = "NOT_FOUND"

    def show(self, world):
        for doc_id, value in list(getattr(self, "_hidden", {}).items()):
            if doc_id.startswith("_status_"):
                self.docs[doc_id[len("_status_"):]]["status"] = value
            else:
                world.client("s3").put_object(Bucket=self.bucket, Key=value[0], Body=value[1])
        self._hidden = {}

    def lookup(self, world, payload):
        """The LookupFunction: one document (`object_key`) or a batch (`object_keys`, `status_only`)."""
        if "object_keys" in payload:
            return {"results": [{"object_key": k, "status": self.docs.get(k, {}).get("status", "NOT_FOUND"),
                                 "timing": {"elapsed": {"total": 4200}}} for k in payload["object_keys"]]}
        key = payload.get("object_key", "")
        return {"status": self.docs.get(key, {}).get("status", "NOT_FOUND"), "object_key": key}
