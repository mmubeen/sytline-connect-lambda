# Moving to the one-function deployment

Earlier shapes of this project were deployed as: **(a)** the plant stack `sytline-connect` with **eight functions** and, unless you supplied a network, a **VPC it created**; and **(b)** a separate **stand-alone** stack `sytline-connect-standalone` (one function, optionally outside any VPC). The deployment is now **one function in a network that already exists**, created by nobody but the network team. This page says how to move without losing data or breaking a network.

What does not change: the jobs table and every record in it (it is retained by the stack from now on), the API paths and bodies, the IAM authorisation, the secret, and what each action does.

## Before you start (everyone)

1. Read [DEPLOYMENT.md](DEPLOYMENT.md) sections 1, 3 and 3a: the stack now **requires** `WORKER_VPC_ID`, `WORKER_SUBNET_IDS` and `WORKER_SG_IDS` (and `API_VPCE_ID` only if you want the private API; empty means no API) and **creates nothing in the network**.
2. Take a copy of the table's state: note the item count (`aws dynamodb scan --table-name <JobsTableName> --select COUNT`); optionally an on-demand backup (`aws dynamodb create-backup`). The count must be the same after the move.
3. Update `deploy.env` from the new `deploy.env.example`. Settings that no longer exist (`WORKER_ROUTE_TABLE_IDS`, `HOST_NETWORK_CIDR`, `PLANT_CIDR_*`, `DYNAMODB_PREFIX_LIST_ID`, `CREATE_VPC_ENDPOINTS`, `CREATE_API_ENDPOINT`, `ATTACH_*`, `ALLOW_PLAIN_HTTP`, `VERIFY_TLS`, `DATABASE_CHECK`) are ignored with a notice; delete them.
4. Make sure the network gives the function what it needs ([DEPLOYMENT.md](DEPLOYMENT.md) section 3a). The old stack may have created Secrets Manager and DynamoDB endpoints for you; with an existing network they must exist already.
5. Callers: a health check now needs `{"action": "health"}`; a payload with no `action` is treated as an IDP event ([EVENT_INTAKE.md](EVENT_INTAKE.md)). Update anything of yours that invokes the function without an `action`.

## Path A: the plant stack was deployed on a network you supplied ("supply" mode)

1. `./deploy.sh --validate`, then `./deploy.sh`. `deploy.sh` prints a notice that the stack still has functions of the earlier shape.
2. In the change set you should see: **create** the one function (`sytline-connect-fn`), **modify** the API (all five routes now point at it) and the role, **remove** the seven old functions (`IntakeFunction`, `StatusFunction`, `ReserveFunction`, `SaveIntakeFunction`, `ReservationStatusFunction`, `WorkerFunction`, `SweeperFunction`) and their roles. The jobs table shows **no change** (it keeps its logical ID and gains only the retain policies). Anything else being removed means stop and ask.
3. Confirm. CloudFormation creates the new function before it removes the old ones.
4. Verify ([DEPLOYMENT.md](DEPLOYMENT.md) section 5), check the item count, and send a status request for an existing upload (`GET /upload-jobs/{id}`).
5. In-flight work: uploads or saves that were `accepted` / `save_accepted` when the old functions were removed are started again by the 5-minute cleanup of the new function; anything that was `running` / `saving` is parked as `needs_reconciliation` by the same rule as before (never replayed). Check `docs/reconciliation-runbook.md`.

## Path B: the plant stack created its own network ("create" mode)

**Do not run the new deployment over it yet.** The new template has no network resources, so the update would try to delete the VPC, subnets, route table, security groups and endpoints that the stack created and that the old functions run in. The network team must first take ownership of that network or provide another.

1. Decide with the network team which network the function will use: a new one, or the existing one made independent of the stack. To keep the VPC the stack created you must take it out of the stack's control **before** the update: set `DeletionPolicy: Retain` on the network resources in the **old** template and update the old stack once (this keeps them when they are later removed from the template), or recreate the network outside the stack and move the plant VPN/routes to it.
2. Put the resulting VPC, subnet and security group IDs (and the `execute-api` endpoint) into `deploy.env`.
3. Then follow Path A.

If you are not sure, deploy the new template as a **new stack with a different name** (change `stack_name` in `src/samconfig.toml`) and the new network settings; run both side by side with live writes off; when the new one is verified, point callers at it and delete the old stack. The jobs table is the one resource both stacks cannot share (it has a stack-generated name): copy the records you need, or finish the old uploads before switching.

## The stand-alone stack

It holds no data. After the new deployment works: `aws cloudformation delete-stack --stack-name sytline-connect-standalone --region us-gov-east-1`, and delete `deploy.standalone.env` and any `ATTACH_*` lines. The stand-alone option (no VPC, https-only ERP) is **retired**: an ERP reachable only from the internet is no longer served by this project, and the https-only rule with its plain-HTTP override no longer exists (the single function talks to the ERP as the plant function always did).

## Rolling back

Keep the old template and `deploy.env` from before the move (they are in git history). Redeploying the old template over the new stack recreates the eight functions and, in create mode, the network; the jobs table is untouched either way.
