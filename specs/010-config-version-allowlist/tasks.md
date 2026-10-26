# Tasks: Process Only Allowed Document Configurations

**Input**: `/specs/010-config-version-allowlist/` (plan.md, spec.md, research.md, data-model.md, contracts/ignored-event.md, quickstart.md)

**Tests**: included (tests-first project; SC-002 requires the existing event tests to pass unchanged).

Format: `- [ ] T### [P?] [US?] description with path`. Paths are relative to the repo root. Run tests with Python 3.12 if 3.13 is not installed: `PYENV_VERSION=3.12.14 python -m pytest ...`.

## Phase 1: Setup

- [X] T001 Add `CONFIG_INVALID = "CONFIG_INVALID"` to `src/syteline/errors.py` and `allowed_config_versions()` to `src/syteline/config.py`: reads `ALLOWED_CONFIG_VERSIONS`, splits on `,`, trims each item, returns `[]` for empty/whitespace-only; every item must match `^[A-Za-z0-9._-]{1,64}$` and no item may be empty (`a,,b` invalid) else raise `IntegrationError(CONFIG_INVALID, "ALLOWED_CONFIG_VERSIONS is not a comma-separated list of 1 to 64 letters, digits, . _ - items", step="config")` (never fall back to allow-all)
- [X] T002 [P] Add parameter `AllowedConfigVersions` (String, default `""`, `AllowedPattern: "^([A-Za-z0-9._-]{1,64}(,[A-Za-z0-9._-]{1,64})*)?$"`, description saying empty = every configuration is processed) and env `ALLOWED_CONFIG_VERSIONS: !Ref AllowedConfigVersions` in `src/template.yaml`; in `src/deploy.sh` validate `ALLOWED_CONFIG_VERSIONS` with the same pattern (`die "ALLOWED_CONFIG_VERSIONS must be empty or a comma-separated list of 1 to 64 letters, digits, . _ - items"`), print `NOTICE: ALLOWED_CONFIG_VERSIONS is empty: every document configuration will be processed.` when empty, pass `"$(q AllowedConfigVersions "${ALLOWED_CONFIG_VERSIONS:-}")"`; add the commented setting with an example (`ChromAlloy-PO`) to `src/deploy.env.example`

## Phase 2: Foundational (blocks the stories)

- [X] T003 Write failing tests first in `tests/unit/test_config.py` for `allowed_config_versions()`: unset/empty/`"  "` -> `[]`; `"ChromAlloy-PO"`; `" A , B "` -> `["A","B"]`; `"a,,b"`, `"bad value"`, `"x"*65`, `"a,"` raise `IntegrationError` with code `CONFIG_INVALID`

**Checkpoint**: setting parsed and validated; T001 makes T003 pass.

## Phase 3: User Story 1 - Only purchase-order documents are processed (P1) MVP

**Goal**: an event whose `detail.input` version is not allowed is ignored before anything is read or recorded.
**Independent Test**: set `ALLOWED_CONFIG_VERSIONS=ChromAlloy-PO`; the sample event is processed; the same event with version `Invoice-v2` is `ignored` and no IDP, ERP or tracker call is made.

- [X] T004 [P] [US1] Tests in `tests/unit/test_eventdoc.py`: with the setting `ChromAlloy-PO` the sample event (`tests/fixtures/sample_event.json`) parses as `process`; with `Invoice-v2` it parses as `ignore` with `reason` containing `Invoice-v2`, `config_version == "Invoice-v2"`, `allowed == ["ChromAlloy-PO"]`; several allowed versions each accepted; matching is exact and case-sensitive (`chromalloy-po` ignored); the version in `detail.output` is not consulted (input allowed + output other -> process; input other + output allowed -> ignore); empty setting -> process whatever the version; a not-SUCCEEDED event and a bad document ID behave as before whatever the version
- [X] T005 [P] [US1] Tests in `tests/unit/test_event_intake.py`: with the setting and a not-allowed version `process()` returns `{"outcome": "ignored", ...}` with `config_version` and `allowed`, `world.untouched()` is true (no IDP read, no SOAP call, no tracker item, no Lambda invoke); an allowed version proceeds exactly as the existing tests; with the setting empty all existing tests pass unchanged
- [X] T006 [US1] In `src/syteline/eventdoc.py` add `_config_version(detail)` (reads `document.config_version` from the parsed `detail.input` via `_object`; returns the string only if it matches `^[A-Za-z0-9._-]{1,64}$`, else `None`) and, in `parse()`, after the document ID has been validated and before the `process` result: `allowed = config.allowed_config_versions()`; if `allowed` and the version is not in it, return `{"action": "ignore", "reason": f"config_version {version!r} is not in the allowed set" (or "the event has no usable config_version" when `None`), "config_version": version, "allowed": allowed}`; import `config` lazily or via `from . import config, errors` (check for a circular import)
- [X] T007 [US1] In `src/handlers/event_intake.py` carry the extra fields of an ignore into the answer and the log: `_out("ignored", reason=..., config_version=..., allowed=...)` and `_log(started, "ignored", reason=..., config_version=..., allowed=...)`, keeping the existing `received/expected/top_level_keys` handling; a `CONFIG_INVALID` raised by `parse` is returned as a refusal through the existing `except IntegrationError` branch (`_refused`), never processed

**Checkpoint**: US1 independently testable.

## Phase 4: User Story 2 - A missing or unreadable version never slips through (P1)

**Goal**: with a restriction on, anything without a usable version is ignored.
**Independent Test**: events with no version, empty, a number, an over-long text, a text with illegal characters: all ignored, nothing touched.

- [X] T008 [P] [US2] Tests in `tests/unit/test_eventdoc.py` and `tests/unit/test_event_intake.py`: with the setting on, `detail.input` without `config_version`, `""`, `123`, `["x"]`, `"x"*65` and `"a b"` each parse as `ignore` with `config_version is None` and reason `the event has no usable config_version` (the bad value is never echoed); with the setting off the same events are processed; an invalid setting (`a,,b`) makes `parse` raise `IntegrationError(CONFIG_INVALID)` and `process` returns a `refused` outcome with nothing contacted
- [X] T009 [US2] Confirm in `src/syteline/eventdoc.py` that the unusable cases of T006 all return `None` from `_config_version` (type check `isinstance(v, str)` first) and that the `detail.input is not valid JSON` case is still refused as `EVENT_INVALID` before the version check (the version check never rescues an invalid input); adjust T006 code if T008 fails

## Phase 5: User Story 3 - The deployer can see and change what is allowed (P2)

**Goal**: deployment validation, notice, health report, log line.
**Independent Test**: malformed list refused by `deploy.sh`; empty prints the notice; health lists the set; an ignored event's log line shows version and set.

- [X] T010 [P] [US3] Tests in `tests/unit/test_deploy_script.py`: default passes `AllowedConfigVersions=""` and prints the NOTICE; `ChromAlloy-PO,Other` is passed with no notice; `a,,b`, `a b`, a 65-character item are refused before any deploy (`not deployed(calls)`); tests in `tests/unit/test_template_single.py`: parameter exists with default `""`, the `AllowedPattern` accepts `""`, `A`, `A,B-2` and rejects `a,,b`, `a b`, `,a`, and `env(template)["ALLOWED_CONFIG_VERSIONS"] == {"Ref": "AllowedConfigVersions"}`
- [X] T011 [P] [US3] Test in `tests/unit/test_health_database.py` (or a new `tests/unit/test_health_versions.py`): `run_health` output contains `allowed_config_versions` equal to the configured list, `[]` when unset; an invalid setting is reported as `[]` plus a warning string rather than raising
- [X] T012 [US3] In `src/syteline/health.py` `run_health` add `out["allowed_config_versions"]` from `cfgmod.allowed_config_versions()` (catch `IntegrationError`: set `[]` and append `"ALLOWED_CONFIG_VERSIONS is invalid"` to `warnings`)
- [X] T013 [P] [US3] Test in `tests/unit/test_event_intake.py` using `caplog`: an ignored-for-version event logs one `event_intake` line with `outcome: ignored`, `config_version` and `allowed`, and no other event or document content (assert none of the sample's bucket names, execution ARN or field values appear)

**Checkpoint**: all three stories done.

## Phase 6: Polish

- [X] T014 [P] Update `docs/EVENT_INTAKE.md`: new section "`ALLOWED_CONFIG_VERSIONS` in `deploy.env`" (what it filters, where the version is read from, exact case-sensitive match, empty = all, the ignored answer, validation, health field, development mode and direct requests not filtered) and add the ignored-for-version row to the outcomes table; mention in `docs/DEPLOYMENT.md` if it lists deploy.env settings
- [X] T015 Run `PYENV_VERSION=3.12.14 python -m pytest tests -q --deselect tests/unit/mock/test_make_guards.py`; confirm every pre-existing test passes unchanged (SC-002)
- [ ] T016 Deployment-only check (unchecked until deployed): with the real IDP event of a document processed by another configuration, confirm `document.config_version` is where the sample shows it and that the event is ignored; confirm `ChromAlloy-PO` documents still reserve numbers

## Dependencies

T001 -> T003 -> US1 (T006 needs T001; T004/T005 written before T006/T007) -> US2 (T009 refines T006) -> US3 (T012 needs T001). T002 is independent of the code and can run beside T001. T010, T011, T013 can run in parallel with each other after T001/T002.

## Parallel examples

- T001 and T002 together.
- US1 tests T004 and T005 together; US3 tests T010, T011, T013 together.

## Strategy

MVP = Phases 1-3 (the filter works and is exercised by tests). US2 hardens the missing/odd-version cases (both P1; deliver together). US3 and polish add visibility. T016 needs the real IDP.
