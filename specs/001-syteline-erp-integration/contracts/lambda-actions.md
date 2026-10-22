# Contract: direct-invoke Lambda actions

Direct Lambda invoke with a JSON event (`src/lambda_function.py`). `action` defaults to `health`. Responses: `{"ok": bool, "action": str, ...}`; failures add `"errors": [{"code","message","field?","step?"}]`. Secrets never appear. Order creation is **not** here any more — see [api.md](api.md).

## health
`{"action":"health","dry_run":false}` → `{ok, dry_run, host, config, stages[]}`. Stages: `dns, tcp, wsdl, token`. Dry run: `load, dns, tcp, wsdl`; sends no credentials and does not log in. There is no database connection of any kind (FR-018).

### Diagnostics (opt-in): `{"action":"health","diagnose":true}`
Adds a `diagnostics` object to the result; `stages`, `ok` and every other key are unchanged, and without the flag nothing is added.
```
"diagnostics": {
  "resolver": {"nameservers": ["..."], "search": ["..."]},
  "local": {"local_address": "..."},
  "erp": {"host": "...", "port": 80, "ok": false,
          "attempts": [{"ok": false, "ms": 3, "error": "OSError: [Errno 16] ...", "errno": 16}, ...],
          "addresses": [], "connections": [{"address": "...", "port": 80, "ok": false, "ms": 5000, "error": "...", "errno": 110}],
          "connected": false},
  "aws": {"host": "secretsmanager[-fips].<region>.amazonaws.com", "port": 443, "ok": true, "...": "same shape as erp"},
  "dns_raw": {"erp": [{"nameserver": "...", "ok": true, "rcode_name": "NOERROR|NXDOMAIN|SERVFAIL|REFUSED", "answers": ["..."], "ms": 3}],
              "aws": [ ...same shape... ]},
  "findings": ["plain-language conclusions"]
}
```
- Contacts only the ERP host and the function's own regional AWS endpoint (name lookup + a TCP connection test, no data sent), reads no secret and uses no credentials; works with `dry_run`.
- `dns_raw` (added 2026-10-21): a raw DNS A query for those same two names sent straight to the function's own nameserver(s) from `resolver` (at most two, 2 s timeout, UDP port 53), reporting the response code or a timeout. It separates "the resolver says no" from "the resolver answers but the function's own lookup fails". On a failed ERP lookup, `erp.gethostbyname` gives a second libc opinion. IPv6 connection failures carry an `expected` note (Lambda has no IPv6 route).
- The ERP lookup is retried up to 3 times on transient errors (every attempt is reported); other lookup errors are not retried. At most 4 addresses are tested per host (IPv4 first).
- `"diagnostics": {"skipped": "..."}` when no ERP address is configured, or when the HTTPS rule (stand-alone version) refused the address (the ERP is then not contacted). `aws` is `null` with `aws_skipped` when the region is not in the environment.

## add_note  (write — requires `SYTELINE_LIVE_WRITES=true`)
`{"action":"add_note","record_type":"co|job","record_key":"…","text":"…","internal":true}` → `{"ok":true,"note_token":123}`. Errors: `VALIDATION`, `NOT_FOUND`, `LIVE_WRITES_DISABLED`, `ERP_REJECTED`.

## get_notes  (read-only)
`{"action":"get_notes","record_type":"co|job","record_key":"…","since":"2026-10-01"}` → `{"ok":true,"notes":[{"token","text","internal","created_at","created_by"}]}`.

Stand-alone (no VPC) deployment only (feature 003): error code `INSECURE_TRANSPORT` is returned by every action, before any ERP access, when the ERP address (or the service address declared in its WSDL) is `http://` and the plain-HTTP override is off; `health` may add `"warnings": ["plain HTTP override is on: credentials cross the network unencrypted"]` when the override is on. The stage list never changes.

`SECRET_STORE_UNREACHABLE` (all actions that read the secret, and `health` outside the dry run): the credentials secret could not be read from Secrets Manager within a few seconds (no network path, denied, missing or malformed); the message names the store and the likely fix.

Error codes: `VALIDATION`, `NOT_FOUND`, `LIVE_WRITES_DISABLED`, `ERP_UNREACHABLE`, `ERP_REJECTED`, `UNKNOWN_ACTION`, `NOT_IMPLEMENTED`.
