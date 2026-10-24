# Run notes

## What was verified, and where (authoring machine: no Docker, Python 3.12, Moto and zeep installed)

| Layer | Verified | How |
|---|---|---|
| Template -> settings, limits, routes, permissions | yes | `tests/unit/mock/test_render_env.py`, `test_parity_static.py` |
| Permission evaluation and AWS-worded denials | yes | `test_policy.py`, `test_actions.py`, `test_edge.py` |
| Edge, control API, faults, reset | yes | `test_edge.py`, `test_control_api.py`, `test_faults.py`, `test_reset.py` |
| ERP mock with the function's real client over HTTP | yes | `test_erp_mock.py` |
| IDP read through the real IDP SDK, over the edge and Moto | yes | `test_idp_seed.py` (needs the IDP checkout) |
| Whole intake in one process (event, IDP, customer, numbers, background save, ERP rows) | yes, with the **web-service** routes standing in for the database | `test_inprocess_e2e.py` |
| Makefile guards | yes | `test_make_guards.py` |
| Real SQL Server (TLS, stand-in routines, `sql.*` faults, proxy) | **no** | `init.sql`, `certs.sh`, `sqlcontrol.py` unit-tested with a fake connection; the engine never ran |
| Lambda runtime image, three-container pool, compose network, no-egress | **no** | `compose.yaml`, `Dockerfile.tools`, `Makefile` written, never started |
| Real-IDP mode: routing, read-only list, re-signing, credentials (missing, expired, refreshed), no-leak, mode refusal | yes, against a stand-in service | `test_realidp_*.py`, `test_edge_real.py`, `test_prepare_real.py`, `test_world_real.py`, `test_inprocess_real_idp.py` |
| Real-IDP mode against real AWS (endpoint names, signature acceptance, real KMS) | **no** | needs your access; `tests/mock_env/test_real_idp.py` is written |
| `tests/mock_env/*` | **no** | skipped without `MOCK_ENV=1`; never run against a live stack |

## Expected adjustments on the first Docker run

- `init.sql` has not met a SQL Server: syntax or permission errors there are the most likely first failure.
- `PYTHONPATH=/var/task:/opt/python` is set because it is not certain the Lambda image adds the layer path itself.
- The `tests/mock_env/test_faults.py` expectations are generic (`ok` false, nothing written); replace them with exact codes once observed.
- `sam build` needs `IDP_REPO`; the function package is built for Python 3.13 / x86_64 wheels, as in a deployment.

## Timings (SC-001, SC-002)

Not measured (T043 pending a Docker machine).
