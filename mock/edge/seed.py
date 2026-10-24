"""The seeded world behind the edge (specs/008 data-model, R7, R13): the AWS-side data in Moto plus the hooks that reset and
load the ERP mock and the SQL Server. `World` is the `backend` the edge asks about health, scenarios, encryption keys and the
IDP lookup function.
"""

import json
import os
import secrets
import time

import boto3
import requests

from . import events, models

LOCK_PK = "LOCK#erp-writes"  # same item the function's ERP-write lock uses (src/syteline/erplock.py)


def secret_name(secret_id):
    """Moto looks secrets up by name; the function passes an ARN of another partition, so use the part after `secret:`."""
    return secret_id.split("secret:", 1)[1] if secret_id.startswith("arn:") and "secret:" in secret_id else secret_id


class World:
    def __init__(self, rendered, moto_url, scenarios_dir, run_dir, erp=None, sql=None, runtime_up=None, idp_seeder=None,
                 mode=None, idp_status=None):
        self.r = rendered
        self.moto_url = moto_url.rstrip("/")
        self.scenarios_dir = scenarios_dir
        self.run_dir = run_dir
        self.erp, self.sql = erp, sql
        self.runtime_up = runtime_up
        self.idp_seeder = idp_seeder
        self.mode = mode or {"mode": "mock"}   # mode.json: real-IDP mode names the document to process
        self.idp_status = idp_status
        self.kms = {}
        self.current = None
        self.table = rendered["env"]["JOBS_TABLE"]
        self.region = rendered["function_arn"].split(":")[3]

    # -- clients ---------------------------------------------------------------------------
    def client(self, name):
        return boto3.client(name, endpoint_url=self.moto_url, region_name=self.region,
                            aws_access_key_id="mock", aws_secret_access_key="mock")

    # -- secrets ---------------------------------------------------------------------------
    def _passwords(self):
        os.makedirs(self.run_dir, exist_ok=True)
        path = os.path.join(self.run_dir, "passwords.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        data = {"SYTELINE_PASS": secrets.token_urlsafe(18), "SQL_PASS": "Aa1!" + secrets.token_urlsafe(18)}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        return data

    def secret_values(self):
        """What the credentials secret holds. Passwords are generated per run directory and never printed."""
        values = {**self._passwords(), "SQL_SERVER": "sqlserver", "SQL_DATABASE": "SyteLine_Mock",
                  "SQL_USER": "sytline_mock", "SQL_PORT": "1433", "SQL_CA_PEM": ""}
        ca = os.path.join(self.run_dir, "ca.pem")
        if os.path.exists(ca):
            with open(ca) as f:
                values["SQL_CA_PEM"] = f.read()
        if self.current:
            values.update(self.current.get("secrets", {}))
        return values

    # -- AWS-side seeding ------------------------------------------------------------------
    def _create_table(self):
        ddb = self.client("dynamodb")
        ddb.create_table(TableName=self.table, BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
        ddb.update_time_to_live(TableName=self.table,
                                TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})

    def _create_secret(self):
        self.client("secretsmanager").create_secret(Name=secret_name(self.r["env"]["SYTELINE_SECRET_ARN"]),
                                                    SecretString=json.dumps(self.secret_values()))

    def _default_kms(self):
        keys = [k for k in self.r["parameters"].get("IdpKmsKeyArns", "").split(",") if k]
        self.kms = {}
        if keys:
            p = self.r["function_arn"].split(":")[1]
            self.kms[f"arn:{p}:s3:::{self.r['parameters']['IdpOutputBucketName']}/"] = keys[0]
            self.kms[f"arn:{p}:dynamodb:{self.region}:{self.r['function_arn'].split(':')[4]}:table/"
                     f"{self.r['parameters']['IdpTrackingTableName']}"] = keys[0]

    def reset(self):
        """Everything back to the starting state (FR-010): AWS data, ERP rows, database rows, locks."""
        requests.post(self.moto_url + "/moto-api/reset", timeout=10).raise_for_status()
        self.current = None
        self._default_kms()
        self._create_table()
        self._create_secret()
        if self.idp_seeder:
            self.idp_seeder.base(self)
        if self.erp:
            self.erp.reset()
        if self.sql:
            self.sql.reset()

    # -- scenarios -------------------------------------------------------------------------
    @property
    def real(self):
        return self.mode.get("mode") == "real"

    def refusal_for(self, name):
        """Why a scenario name cannot be used in this mode (the two modes are never mixed), or None."""
        mock_names = self._mock_scenarios()
        if self.real and name in mock_names:
            return "real-IDP mode is on: mock scenarios are not available (stop and start without MOCK_IDP=real)"
        if not self.real and name == "real-idp":
            return "real-IDP mode is off (start with MOCK_IDP=real and its settings)"
        return None

    def _mock_scenarios(self):
        if not os.path.isdir(self.scenarios_dir):
            return []
        return sorted(n for n in os.listdir(self.scenarios_dir)
                      if os.path.exists(os.path.join(self.scenarios_dir, n, "scenario.json")))

    def scenario_names(self):
        if self.real:
            return ["real-idp"]
        if not os.path.isdir(self.scenarios_dir):
            return []
        return sorted(n for n in os.listdir(self.scenarios_dir)
                      if os.path.exists(os.path.join(self.scenarios_dir, n, "scenario.json")))

    def load(self, name):
        if self.real:
            if name != "real-idp":
                raise KeyError(name)
            self.current = None
            return events.build_event(self.mode["document"])
        sc = models.load_scenario(os.path.join(self.scenarios_dir, name))
        self.current = sc
        # the secret is rewritten with the scenario's extra settings
        self.client("secretsmanager").put_secret_value(SecretId=secret_name(self.r["env"]["SYTELINE_SECRET_ARN"]),
                                                       SecretString=json.dumps(self.secret_values()))
        if self.erp:
            self.erp.load(sc["erp"])
        if self.sql and sc["sql"].strip():
            self.sql.run(sc["sql"])
        if self.idp_seeder:
            self.idp_seeder.document(self, sc)
        return sc["event"]

    # -- what the edge asks ----------------------------------------------------------------
    def kms_key_for(self, resource):
        return next((k for prefix, k in self.kms.items() if resource.startswith(prefix)), None)

    def lookup(self, payload):
        return self.idp_seeder.lookup(self, payload) if self.idp_seeder else {}

    def fault_added(self, fault):
        if fault["id"] == "lock.held":
            now = int(time.time())
            self.client("dynamodb").put_item(TableName=self.table, Item={
                "pk": {"S": LOCK_PK}, "holder": {"S": "mock-other-save"}, "action": {"S": "work_save"},
                "acquired_at": {"N": str(now)}, "expires_at": {"N": str(now + 3600)}, "ttl": {"N": str(now + 7200)}})
        elif fault["id"] == "idp.not_found" and self.idp_seeder:
            self.idp_seeder.hide(self)
        elif self.erp and fault["id"].startswith("erp."):
            self.erp.fault(fault["id"], fault.get("params"), fault.get("remaining"))
        elif self.sql and fault["id"].startswith("sql."):
            self.sql.fault(fault["id"], fault.get("params") or {})

    def fault_cleared(self, ids):
        if "lock.held" in ids:
            self.client("dynamodb").delete_item(TableName=self.table, Key={"pk": {"S": LOCK_PK}})
        if "idp.not_found" in ids and self.idp_seeder:
            self.idp_seeder.show(self)
        if self.erp and any(i.startswith("erp.") for i in ids):
            self.erp.clear_faults()
        if self.sql and any(i.startswith("sql.") for i in ids):
            self.sql.clear_faults()

    def erp_calls(self, since=0):
        return self.erp.calls(since) if self.erp else []

    def health(self):
        out = {}
        try:
            requests.get(self.moto_url + "/moto-api/", timeout=2)
            out["moto"] = "up"
        except requests.RequestException:
            out["moto"] = "down"
        out["idp"] = (self.idp_status() if self.idp_status else "up") if self.real else "mock"
        if self.erp:
            out["erp"] = "up" if self.erp.healthy() else "down"
        if self.sql:
            out["sql"] = "up" if self.sql.healthy() else "down"
        if self.runtime_up:
            out["fn"] = "up" if self.runtime_up() else "down"
        return out
