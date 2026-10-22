# Quickstart: validating SyteLine ERP Integration

No step writes to the live ERP unless the ERP owner has explicitly approved that test (FR-016). Order follows the instruction sheet, section 6.

## 1. Offline
`pip install -r src/requirements.txt -r src/requirements-dev.txt && pytest tests/unit tests/contract` — fakes for the web service (SOAP), DynamoDB and Lambda; there is no SQL anywhere. Must cover: validation (customer details, `uploaded_by`, `confirmed`), no/ambiguous customer match, diffgram row-level rejection inside HTTP 200, read-back mismatch, every uncertainty case → `needs_reconciliation`, duplicate `request_ref`, ref reuse with a different body (409), live-writes disabled stops before number allocation (every `invoke` and `insert` is gated), dry run sends no credentials and logs in to nothing.

## 2. Network (from the actual GovCloud worker, read-only)
Invoke `health` (non-dry) from the deployed worker VPC: DNS for `cctsl01.cgtc.com` resolves to the plant address, SOAP reachable on port 80, token stage passes. Check both VPN tunnels, Palo Alto policy logs, routes and return traffic. A read-only web-service query on the customer-address collection confirms the account and the collection name, and discovery confirms that `NextCoSp`, `NextWOSp` and `CreateSpecificNoteSp` are callable as web-service methods (no write is made).

## 3. API (approved client)
- Authenticated call to `POST /confirmed-orders` with `SYTELINE_LIVE_WRITES=false`: expect 202, then `GET /upload-jobs/{id}` → `failed` / `LIVE_WRITES_DISABLED` after a successful customer match (nothing written, no number allocated).
- Repeat the same body: 200 `duplicate:true`; change one field: 409.
- Unapproved network or identity: denied by the resource policy / IAM.
- Missing `uploaded_by`, `confirmed:false`, or a customer number instead of customer details: 400.

## 4. Failure handling (simulated: VPN loss, SOAP rejection in HTTP 200, timeout after a possible insert)
None may cause an automatic duplicate. Uncertain cases end in `needs_reconciliation` with `possibly_written` listed; the sweeper never re-drives them.

## 5. Live ERP (only with explicit written approval from the ERP owner)
Set `SYTELINE_LIVE_WRITES=true` for the approved window only. Agree beforehand: the single test order, the customer to use, how its CO/job rows will be reconciled and cleaned up, and who performs the cleanup. Set the flag back to `false` afterwards. Record the outcome here.
