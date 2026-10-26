# Research: Allowed Configuration Versions

**R1. Where the version is.** The sample event's `detail.input` is a JSON text whose `document` object holds `config_version` (`"ChromAlloy-PO"`) next to `config_revision`. Decision: read `document.config_version` from the already parsed `detail.input` object (`eventdoc._object`). A top-level `config_version` is not consulted. Alternative: search the whole text with a regex (rejected: fragile, could match inside other fields).

**R2. Where the check runs.** Decision: inside `eventdoc.parse`, after the event is recognised as a SUCCEEDED status change and the document ID is validated, before the function returns `process`. Rationale: `parse` is called before the IDP read, the claim and any reservation (event_intake.process), so an ignored event touches nothing (FR-003, FR-005). Alternative: check in `event_intake.process` after parsing (rejected: a second place that could drift).

**R3. Not-allowed answer.** Decision: reuse `{"action": "ignore"}` with `reason` and extra fields `config_version` (sanitised) and `allowed`. `event_intake` already turns an ignore into an `ignored` outcome and logs it. Alternative: a new refused outcome with an error code (rejected by the clarification: quiet ignore).

**R4. Setting format.** Decision: `ALLOWED_CONFIG_VERSIONS`, comma-separated; items trimmed; each 1-64 of `[A-Za-z0-9._-]`; empty or whitespace-only = off; an empty item (`a,,b`) or illegal item is invalid. Runtime: an invalid value makes `config.allowed_config_versions()` raise `IntegrationError(CONFIG_INVALID)` rather than silently allow everything; `parse` is called inside the intake's error handling so the event is refused (not processed). `deploy.sh` and the template (`AllowedPattern`) refuse it earlier. Alternative: a JSON list (rejected: awkward in a deploy parameter).

**R5. Missing or odd version.** Decision: with a restriction on, not-a-string, empty, longer than 64 or containing characters outside the pattern is "not allowed" with reason `no usable config_version`; the value is never echoed unless it passes the pattern. Without a restriction the version is not read at all.

**R6. Matching.** Decision: exact, case-sensitive, whole value; the event's value is not trimmed.

**R7. Visibility.** Decision: the ignored answer and its log line carry `config_version` (only if it passes the pattern, else `"<unusable>"`) and `allowed`; the health answer gets `allowed_config_versions`; `deploy.sh` prints a notice when the list is empty.

**R8. Other paths.** Decision: direct reserve/save requests (no event) and development mode are not filtered; documented.
