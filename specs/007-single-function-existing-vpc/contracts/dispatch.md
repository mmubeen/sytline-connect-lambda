# Contract: what the one function runs

Entry point: `lambda_function.lambda_handler(event, context)`, function name `<stack>-fn`.

## Routing (first match wins)
| # | Invocation | Runs | Time budget |
|---|---|---|---|
| 1 | `action` given and non-empty | that action (below); unknown -> `UNKNOWN_ACTION` | per action |
| 2 | API Gateway event (`httpMethod` and `resource` present) | the route's handler (below); unknown route -> `{"statusCode": 404}` | per route |
| 3 | anything else | `document_event` (feature 006: IDP event; a payload that is not an IDP event is refused `EVENT_INVALID` naming `"action": "health"`) | 55 s |

## Actions
| `action` | Payload | Behaviour | Budget |
|---|---|---|---|
| `health` | `dry_run`, `diagnose` as today | connectivity check; database stages decided by the operation routes only | 55 s |
| `add_note`, `get_notes` | as today | unchanged | 55 s |
| `document_event` | the event, or `event` holding it | feature 006 intake | 55 s |
| `work_upload` | `{"upload_id": …}` | run one accepted upload under the ERP-write lock (never replays) | remaining |
| `work_save` | `{"reservation_id": …}` | run one accepted save under the ERP-write lock (never replays) | remaining |
| `sweep` | none | re-drive `accepted` / `save_accepted`, park stale `running` / `saving` (as today) | remaining |

`work_upload`, `work_save` and `sweep` are internal: started by the function itself (asynchronously, no retries) or by the schedule; invoking them by hand is allowed for operators with invoke permission.

## API routes (unchanged paths, methods, IAM auth, bodies)
| Method and resource | Handler | Budget |
|---|---|---|
| `POST /confirmed-orders` | `handlers.intake` | 12 s |
| `GET /upload-jobs/{id}` | `handlers.status` | 8 s |
| `POST /job-reservations` | `handlers.reserve` | 25 s |
| `POST /job-reservations/{id}/save` | `handlers.save_intake` | 12 s |
| `GET /job-reservations/{id}` | `handlers.reservation_status` | 8 s |

A budget is the most time an action may use; the context handed to the handler reports `min(real remaining, budget - elapsed)` so time checks stop work before API Gateway's limit.

## The ERP-write lock
`work_upload` and `work_save`: acquire `LOCK#erp-writes` (poll 2 s, wait at most remaining - 60 s); not acquired -> return `{"deferred": true}` without claiming anything (the sweeper re-drives); acquired -> claim and run as today -> release in a `finally` if still the holder.

## Removed behaviour
`ACTION_UNAVAILABLE`, the https-only guard (`INSECURE_TRANSPORT` from the settings `SYTELINE_REQUIRE_HTTPS`/`SYTELINE_ALLOW_HTTP`), `SYTELINE_ATTACHED`, `SYTELINE_DB_CHECK`.
