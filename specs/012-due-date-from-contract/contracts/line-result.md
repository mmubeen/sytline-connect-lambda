# Contract: due date in the save result

Each entry of `results[]` in the save outcome gains:

| Field | Type | Meaning |
|-------|------|---------|
| `due_date` | string \| null | The ERP datetime written to the line and used as the job's start and end; null when none could be computed |
| `due_source` | `"contract"` \| `"default"` \| `"none"` | Where the due period came from |

When `due_source` is `none`, the line's `warnings` gains one entry, e.g. `no due date: no due period in the customer contract for "R* 3810727-6" or the company default (the line was saved without one)`, or `no due date: the received date is not a valid date`. A document `due_date` never appears in the result and is never written. No request field changes.
