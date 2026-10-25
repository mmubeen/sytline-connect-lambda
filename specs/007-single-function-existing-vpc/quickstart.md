# Quickstart: validating the single-function deployment

## 1. Shape (no AWS)
`PYENV_VERSION=3.12 pytest tests/unit/test_template_single.py`: exactly one function, no `AWS::EC2::*` resource, five routes and the schedule on it, three required network parameters plus the API endpoint, table retained. `cfn-lint src/template.yaml` and `sam validate --lint` are clean.

## 2. Settings
Copy `src/deploy.env.example` to `src/deploy.env`; fill the ERP settings, `WORKER_VPC_ID`, `WORKER_SUBNET_IDS`, `WORKER_SG_IDS`, and `API_VPCE_ID` only if you want the API (empty = no API). `./deploy.sh --validate` passes; with one of the three network values missing or malformed it stops before building and names it.

## 3. Deploy and check
`./deploy.sh`; the change set creates one function and no network resource. `aws cloudformation list-stack-resources --stack-name sytline-connect` shows one `AWS::Lambda::Function`, no `AWS::EC2::*`.

## 4. Every action through the one function
`{"action":"health","dry_run":true}`; `get_notes`; the sample IDP event with no action; the five API routes through the private API; `{"action":"sweep"}` by hand.

## 5. Serialization and responsiveness (simulated ERP or a test window)
Start two saves close together: their ERP writes do not overlap (log timestamps / `LOCK#erp-writes` holder changes). While one runs, a health check and a status request answer within seconds.

## 6. Moving from the old shape
Follow `docs/MIGRATION.md`; afterwards the jobs table item count equals the count before, the seven old functions are gone, and the leftover old stack named in the migration guide has been deleted with `aws cloudformation delete-stack`.
