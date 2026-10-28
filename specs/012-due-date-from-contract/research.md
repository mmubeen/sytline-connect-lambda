# Research: Due Date from the Customer Contract

**R1. The rule (demo).** `lookups.lookup_contract_due`: first `SLItemcusts` where `CustNum` (trimmed) and `Item` match, property `DuePeriod`, used when it parses to an integer > 0 (UM also read there; not used here, per the clarification); else `SLCoparms.DuePeriod` (first row, no filter) when > 0; else none. `formatting.compute_due_date(order_date, days)` = order date + days, formatted as the ERP datetime; invalid date or days <= 0 gives none. **Decision**: port exactly. **Alternatives**: item-master-own due period (not in the demo; later).

**R2. Which date is the base.** The demo uses `date_received`. The save already has `received_date` (falling back to `order_date`, then today) in `rowbuild._received`. **Decision**: use the same value so the order date and the due base agree.

**R3. Filter on the customer.** The demo filters `LTRIM(CustNum) = '<cust>'`. The save pads `CustNum` to 7 (`rowbuild.rjust`) everywhere else and the existing `Item` filter is exact and unpadded for the repair code (probe_item finding). **Decision**: `CustNum = <padded 7>` AND `Item = <repair code>`; the demo's `LTRIM` form is the documented fallback if the padded form returns nothing in the real ERP (to confirm with a probe).

**R4. Document's own due date.** Clarified: ignored. `rowbuild.co_line/job_row` read `ln["due_date"]`, so the save passes them an effective line whose `due_date` is the computed one (or absent). **Alternatives**: change the rowbuild signatures (wider diff).

**R5. Failure handling.** Lookups use `read_back`, catch every error, warn on the line, never raise (as `itemdesc.lookup`). An unreachable ERP surfaces through the writes that follow, as today.

**R6. Preflight.** `due_date` on `CCT_Coitems` is checked only when a line has one today; it must now be checked always, since the save always tries to write it. The contract/default property names are NOT preflighted (a wrong name only costs the date, with a warning).

**R7. Job dates.** `job_row` already uses `ln["due_date"]` for `JschStartDate/EndDate`, else the job date; no change.

**R8. Caching.** One read per distinct (customer, item) and one default read per save, held in the workflow object.
