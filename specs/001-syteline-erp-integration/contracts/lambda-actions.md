# Contract: direct-invoke Lambda actions

Direct Lambda invoke with a JSON event (`scr/lambda_function.py`). `action` defaults to `health`. Responses: `{"ok": bool, "action": str, ...}`; failures add `"errors": [{"code","message","field?","step?"}]`. Secrets never appear. Order creation is **not** here any more — see [api.md](api.md).

## health
`{"action":"health","dry_run":false}` → `{ok, dry_run, host, config, stages[]}`. Stages: `dns, tcp, wsdl, token` (+ optional `sql` TCP check when SQL settings exist and not dry run). Dry run: `load, dns, tcp, wsdl`; never opens SQL, never sends credentials.

## add_note  (write — requires `SYTELINE_LIVE_WRITES=true`)
`{"action":"add_note","record_type":"co|job","record_key":"…","text":"…","internal":true}` → `{"ok":true,"note_token":123}`. Errors: `VALIDATION`, `NOT_FOUND`, `LIVE_WRITES_DISABLED`, `ERP_REJECTED`.

## get_notes  (read-only)
`{"action":"get_notes","record_type":"co|job","record_key":"…","since":"2026-10-01"}` → `{"ok":true,"notes":[{"token","text","internal","created_at","created_by"}]}`.

Error codes: `VALIDATION`, `NOT_FOUND`, `LIVE_WRITES_DISABLED`, `ERP_UNREACHABLE`, `ERP_REJECTED`, `UNKNOWN_ACTION`, `NOT_IMPLEMENTED`.
