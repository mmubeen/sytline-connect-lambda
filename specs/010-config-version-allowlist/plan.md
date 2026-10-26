# Implementation Plan: Process Only Allowed Document Configurations

**Branch**: `010-config-version-allowlist` | **Date**: 2026-10-26 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/010-config-version-allowlist/spec.md`

## Summary

A deployment setting `ALLOWED_CONFIG_VERSIONS` (template parameter `AllowedConfigVersions`, comma-separated, empty = no restriction) names the IDP configuration versions to process. `eventdoc.parse` reads `document.config_version` from `detail.input` and, when a restriction is set and the version is not in it (or is missing or invalid), returns the existing `{"action": "ignore", ...}` answer with the version found and the allowed set. Because `parse` runs before the IDP read, the claim and any ERP call, nothing else happens. `deploy.sh` and the template validate the list and warn when it is empty; the health answer lists the set in force. See [research.md](research.md), [data-model.md](data-model.md), [contracts/ignored-event.md](contracts/ignored-event.md), [quickstart.md](quickstart.md).

## Technical Context

**Language/Version**: Python 3.13 (Lambda), SAM template

**Primary Dependencies**: none added

**Storage**: none (no tracker entry for an ignored event)

**Testing**: pytest unit tests (`tests/unit/test_eventdoc.py`, `test_event_intake.py`, `test_config.py`, `test_deploy_script.py`, `test_template_single.py`, health tests); the sample event fixture already carries `ChromAlloy-PO`

**Target Platform**: AWS Lambda (GovCloud), single function

**Project Type**: single Lambda project

**Performance Goals**: no change; the check is one dictionary lookup on an already-parsed object

**Constraints**: nothing else in the event is trusted or kept (eventdoc rule); the value echoed in answers and logs is shortened and limited to safe characters; empty setting = behaviour unchanged

**Scale/Scope**: ~6 source files, ~20 tests

## Constitution Check

`.specify/memory/constitution.md` is the unfilled template; standing project rules are the gates.

| Rule | Status |
|------|--------|
| Live ERP writes OFF by default | Pass: unchanged; the filter only reduces what is processed |
| Credentials/document content never logged | Pass: only the short, sanitised version string and the allowed set are logged |
| One function, no network resources | Pass: a parameter and an env var only |
| Tests first, fakes layer kept | Pass |
| Existing behaviour unchanged when the setting is empty | Pass: tested with the existing event suite |

Re-check after Phase 1: no violations.

## Project Structure

### Documentation (this feature)

```text
specs/010-config-version-allowlist/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/ignored-event.md
└── tasks.md             # /speckit-tasks
```

### Source Code

```text
src/
├── template.yaml            # AllowedConfigVersions param -> ALLOWED_CONFIG_VERSIONS
├── deploy.sh, deploy.env.example   # validation + notice when empty
├── syteline/
│   ├── config.py            # allowed_config_versions() -> list
│   ├── eventdoc.py          # config_version(detail) + the allow check inside parse()
│   └── health.py            # report the allowed versions
└── handlers/event_intake.py # log the version found + the allowed set on an ignored event
tests/unit/                  # test_eventdoc.py (new cases), test_event_intake.py, test_config.py, test_deploy_script.py, test_template_single.py, health test
docs/EVENT_INTAKE.md         # the setting and the ignored answer
```

**Structure Decision**: the check lives in `eventdoc.parse` so it is the first thing that happens to an event and cannot be bypassed by a later stage.

## Complexity Tracking

No violations.
