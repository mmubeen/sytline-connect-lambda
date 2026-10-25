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
| `LOG_EVENTS=true`, line within 200,000 bytes | `whole` | the whole event as received, no cap (header credentials still masked) |
| `LOG_EVENTS=true`, line would exceed 200,000 bytes | `whole` | `{"truncated": true, "chars": N, "head": "<start of the event JSON>", "copy": {"bucket": "...", "key": "events/..."}}`; the full masked event is in that S3 object. If it could not be stored: `"copy_error": "<ExceptionType>"` (or `"not_configured"`) instead of `copy` |
| payload is not an object | as set | `{"payload_type": "<type>", "chars": n}` (a JSON string is parsed and treated as an object) |

Guarantees: never raises; never changes the result; a failure leaves one WARNING `event_entry could not be built: <ExceptionType>`.
Setting: `LOG_EVENTS` (`true`/`false`, default `false`) from the template parameter `LogEvents`. With it `true` the log holds document and order content.
