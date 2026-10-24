# Contract: entry log line

Written once on entry to the handler, for every invocation, before routing. INFO level, one JSON object.

```json
{"type": "event_entry", "mode": "redacted", "event": {"version": "0", "detail-type": "Step Functions Execution Status Change",
  "source": "aws.states", "detail": {"status": "SUCCEEDED", "name": "159112.pdf-33b4...",
  "input": {"redacted": true, "chars": 11204, "document_id": "159112.pdf"}, "output": {"redacted": true, "chars": 12981}}}}
```

| Situation | `mode` | `event` |
|---|---|---|
| default | `redacted` | event with `input`, `output`, `body`, `text` values replaced by `{"redacted": true, "chars": n}`; header credentials masked |
| `LOG_EVENTS=true`, within the cap | `whole` | the event as received (header credentials still masked) |
| `LOG_EVENTS=true`, over `LOG_EVENTS_MAX_CHARS` (8000) | `whole` | `{"truncated": true, "chars": N, "head": "<first 8000 characters>"}` |
| payload is not an object | as set | `{"payload_type": "<type>", "chars": n}` (a JSON string is parsed and treated as an object) |

Guarantees: never raises; never changes the result; a failure leaves one WARNING `event_entry could not be built: <ExceptionType>`.
Setting: `LOG_EVENTS` (`true`/`false`, default `false`) from the template parameter `LogEvents`. With it `true` the log holds document and order content.
