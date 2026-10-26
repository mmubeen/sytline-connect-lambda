# Contract: events and the allowed set

**Setting**: parameter `AllowedConfigVersions` (deploy.env `ALLOWED_CONFIG_VERSIONS`) -> env `ALLOWED_CONFIG_VERSIONS`. Example `ChromAlloy-PO` or `ChromAlloy-PO,ChromAlloy-PO-2`. Empty = off.

**Event answer when not allowed** (`document_event` action / event intake):
```json
{"outcome": "ignored", "reason": "config_version 'Invoice-v2' is not in the allowed set",
 "config_version": "Invoice-v2", "allowed": ["ChromAlloy-PO"]}
```
Missing/unusable: `"reason": "the event has no usable config_version"`, `"config_version": null`. Nothing was read, reserved or recorded. HTTP/Lambda result is a success.

**Order of checks** (unchanged earlier ones first): not a status change -> ignored; not SUCCEEDED -> ignored; no/invalid document ID -> refused; **version not allowed -> ignored**; then as today.

**Health** (`action: health`): adds `"allowed_config_versions": ["ChromAlloy-PO"]` (empty list = all processed).

**Log line** of an ignored event: `{"type": "event_intake", "outcome": "ignored", "reason": ..., "config_version": ..., "allowed": [...]}`; no other event content.

**Unchanged**: reserve/save requests that name a document ID; development mode; everything when the setting is empty.
