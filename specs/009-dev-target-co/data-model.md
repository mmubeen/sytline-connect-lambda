# Data Model: Development Mode

- **Development target** (setting): `DEV_TARGET_CO`, empty or 1-10 digits. Padded to 10 for ERP filters. Not stored in the ERP by this feature.
- **Reservation item** (existing): new optional attribute `dev_target` (string) set when created in dev mode.
- **Event claim item** (existing, `eventstore`): a dev run writes it unconditionally (overwrites an earlier claim); new attribute `dev_target`.
- **Save result**: per-line `action` in `{"inserted","updated"}` (absent outside dev mode), order-level `co_action`, `surplus_lines` (list of line numbers on the target the document does not have), `development: {target}`.
- **State transitions**: unchanged. A dev run is an ordinary reservation (`reserving -> reserved -> save_accepted -> saving -> saved|partly_saved|failed|needs_reconciliation`).
- **Validation**: target exists in `SLCos`; its `CustNum` equals the resolved customer's padded `CustNum`.
