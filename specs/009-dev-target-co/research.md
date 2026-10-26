# Research: Development Mode

**R1. Setting.** Decision: new `DEV_TARGET_CO` (template parameter `DevTargetCo`), replacing `CO_NUMBER`. Rationale: spec says it supersedes it; two settings meaning "fixed CO" would conflict, and the new one needs different save behaviour. Alternatives: keep `CO_NUMBER` and add a boolean (two settings that must agree); per-request switch (rejected: a caller could turn it on, spec assumes deployment-only).

**R2. Job.** Decision: `JOB_NUMBER` unchanged; in dev mode a job that exists is updated, otherwise inserted. Rationale: spec clarification. Alternative: leave the job insert-only (fails on the second run).

**R3. Overwrite mechanism.** Decision: read the target's header and lines by `CoNum`; header: `client.update(SLCos, {CoNum}, values)`; each line: update by `{co_num, co_line}` on the line IDO if it exists else insert; surplus lines untouched and reported as a warning. Rationale: `client.update` already exists with the safety classification of `insert`. Alternative: delete-and-reinsert (rejected: never delete). Risk: the real ERP's acceptance of updates and its key properties are unverified; the first run's ERP message will show.

**R4. Target checks.** Decision: before any write, `SLCos` read of the target (padded filter): none -> refuse `DEV_TARGET_MISSING`; `CustNum` differs from the resolved customer (padded compare) -> refuse `DEV_TARGET_CUSTOMER_MISMATCH` naming both. Done at reservation (nothing consumed anyway) and again at save. Alternative: check only at save (a reservation would be recorded for a target that cannot be used).

**R5. Tracker bypass.** Findings: the per-request index already uses a unique `request_ref` per event attempt, and the document marker is overwritten unconditionally, so only two things block: the event intake's `_final(first)` "already processed" short-circuit and `EventStore.claim`'s `attribute_not_exists`. Decision: in dev mode skip the first, and claim with an unconditional put (`claim_dev`), keeping the item so `events.record` still works. The save's state machine (`save_accepted -> saving`) is per reservation and a dev run always has a fresh reservation, so it needs no change. Direct API callers pass their own `request_ref`; a repeated ref is the caller's choice and stays a duplicate.

**R6. Marking.** Decision: reservation items gain `dev_target`; responses carry `"development": {"target": "<co>"}`; log lines gain `dev_target`. The deploy script warns while the setting is set.

**R7. Validation.** Decision: `DEV_TARGET_CO` must be 1-10 digits (the CO number width), checked in `deploy.sh`, the template (`AllowedPattern`) and at runtime (invalid -> treated as an error `DEV_TARGET_INVALID`, nothing reserved), so a typo never falls back to normal mode silently.
