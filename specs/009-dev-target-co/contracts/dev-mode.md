# Contract: Development Mode

**Setting**: template parameter `DevTargetCo` (deploy.env `DEV_TARGET_CO`) -> function env `DEV_TARGET_CO`. Empty = off. Pattern `^[0-9]{1,10}$`.

**Reserve / event intake (when on)**: no `NextCoSp`; reservation `co_num` = target; response adds `"development": {"target": "<co>"}`. Event intake never returns `already_processed` for a document because of an earlier entry. Failures: `DEV_TARGET_MISSING` (step `check_target`), `DEV_TARGET_CUSTOMER_MISMATCH` (step `check_target`, message names both customers), `DEV_TARGET_INVALID`. None consumes a number or writes to the ERP.

**Save (when on)**: before the first write re-checks the target. Header: update. Line i: update if present else insert. Job: update if `JOB_NUMBER` set and the job exists, else insert. Results carry `action` per line and `co_action: "updated"`; surplus target lines are listed in `surplus_lines` and a warning. A refusal part-way ends `needs_reconciliation` as today.

**Unchanged when off**: every contract of features 001-008.
