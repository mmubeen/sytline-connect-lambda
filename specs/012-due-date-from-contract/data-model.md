# Data Model: Due Date from the Customer Contract

- **Due period** (days): integer > 0, from the contract entry, else the company default; anything else (blank, text, zero, negative) is "none" for that source.
- **Contract entry** (`SLItemcusts`): `CustNum` (7 wide, right-justified), `Item` (repair code, e.g. `R* 3810727-6`), `DuePeriod`. Read only.
- **Company default** (`SLCoparms`): first row, `DuePeriod`. Read only.
- **Computed due date**: received date + due period days; ERP datetime (`YYYY-MM-DDT00:00:00-04:00`/`-05:00`, Eastern). Absent when the date or period is unusable.
- **Line result additions**: `due_date` (string or null), `due_source` (`contract` | `default` | `none`).

State: none. Nothing stored beyond the existing save outcome.
