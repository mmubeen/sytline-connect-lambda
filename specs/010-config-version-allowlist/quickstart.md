# Quickstart: Allowed Configuration Versions

1. `deploy.env`: `ALLOWED_CONFIG_VERSIONS=ChromAlloy-PO`; deploy (empty would print a notice that every configuration is processed).
2. Send `sample_events.json` (version `ChromAlloy-PO`): processed as before.
3. Edit the version inside `detail.input` to `Invoice-v2` and send it: `outcome: ignored`, `config_version: Invoice-v2`, `allowed: ["ChromAlloy-PO"]`; no IDP read, ERP call or tracker item (check the log line and the jobs table).
4. Remove `config_version` from `detail.input`: ignored, `config_version: null`.
5. `ALLOWED_CONFIG_VERSIONS="a,,b"` or `"bad value"`: `deploy.sh` refuses before deploying.
6. Clear the setting and redeploy: every event is processed again.
7. `action: health`: `allowed_config_versions` shows what is in force.

Automated: `pytest tests/unit/test_eventdoc.py tests/unit/test_event_intake.py tests/unit/test_config.py tests/unit/test_deploy_script.py tests/unit/test_template_single.py`.
