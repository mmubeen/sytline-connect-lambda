# Quickstart: validating the due date

1. Unit tests: `PYENV_VERSION=3.12.14 python -m pytest tests/unit/test_duedate.py tests/unit/test_savewf.py tests/unit/test_preflight.py -q`.
2. Deployment check (read-only), before a dev save: `{"action": "probe_properties", "ido": "SLItemcusts", "properties": ["CustNum", "Item", "DuePeriod"]}` and `{"action": "probe_properties", "ido": "SLCoparms", "properties": ["DuePeriod"]}`; all must be under `exists`. Also confirm the property `due_date` on `CCT_Coitems`.
3. Dev save (target `DEV_TARGET_CO`) of a document whose customer and item have a contract `DuePeriod` of N: the line result shows `due_source: "contract"` and `due_date` = received date + N days; the order line and the job's start/end in the ERP show it.
4. Repeat with an item that has no contract: `due_source: "default"` (company default) or `"none"` with a line warning.
