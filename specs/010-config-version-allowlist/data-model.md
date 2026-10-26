# Data Model: Allowed Configuration Versions

- **Allowed set**: `ALLOWED_CONFIG_VERSIONS`; list of strings; each matches `^[A-Za-z0-9._-]{1,64}$`; no empty items; empty list = no restriction. Not stored anywhere but the function's environment.
- **Event configuration version**: `document.config_version` of the parsed `detail.input`; used only when the set is non-empty; "found" iff it is a string matching the item pattern.
- **Parse result** (`eventdoc.parse`): unchanged for `process`; for a not-allowed event `{"action": "ignore", "reason": str, "config_version": str|None, "allowed": [str]}`.
- **No new stored data**: an ignored event writes nothing (no claim, no reservation); responses and log lines carry the two fields above.
- **Validation**: the set is validated at deploy time, at template level and at runtime (invalid -> refused, never "allow all").
