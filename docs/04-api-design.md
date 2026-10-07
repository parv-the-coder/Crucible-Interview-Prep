# API design

FastAPI, versioned under `/api/v1`. Interactive docs at `/docs` in every
environment except production, where they are disabled, an OpenAPI schema is a
map of your attack surface.

## Endpoints

### Auth
| | |
| --- | --- |
| `POST /auth/signup` | Create an account |
| `POST /auth/signin` | Exchange credentials for a token pair |
| `POST /auth/refresh` | Rotate. The presented token is revoked |
| `POST /auth/signout` | End this session only |
| `POST /auth/signout-all` | End every session |
| `GET /auth/me` | Current user |

### Questions
| | |
| --- | --- |
| `GET /questions` | Browse, filtered by topic/type/difficulty/search |
| `GET /questions/topics` | Topics with counts, for the filter sidebar |
| `GET /questions/{id}` | Detail, **sample test cases only** |
| `POST /questions` | Create (admin) |
| `PATCH /questions/{id}` | Update (admin) |
| `DELETE /questions/{id}` | Archive (admin), never a hard delete |

### Submissions
| | |
| --- | --- |
| `POST /submissions` | Submit. Returns `202` |
| `GET /submissions` | My submissions |
| `GET /submissions/{id}` | Detail, including AI review once it exists |
| `POST /submissions/hint` | One nudge, counted against the daily AI budget |
| `GET /submissions/languages` | Supported runtimes |

### Sessions
| | |
| --- | --- |
| `POST /sessions` | Start a timed test |
| `GET /sessions/{id}` | Detail with `seconds_remaining` |
| `PUT /sessions/{id}/items/{item}/draft` | Autosave, not a submission |
| `POST /sessions/{id}/violations` | Report a proctoring event |
| `POST /sessions/{id}/submit` | Finish; queues every draft |
| `GET /sessions/{id}/result` | Live result, fills in as grading completes |

### Rooms
| | |
| --- | --- |
| `POST /rooms` | Create; returns a join code |
| `POST /rooms/join` | Join by code |
| `GET /rooms/{id}/replay` | Full event log for playback |
| `POST /rooms/{id}/end` | End (interviewer only) |
| `PUT /rooms/{id}/feedback` | Private interviewer notes |
| `WS /ws/rooms/{id}` | The live connection |

### Operational
`GET /health/live`, `GET /health/ready`.

## Decisions worth defending

### 202, not 200, for submissions

The work has not happened yet. Returning `200` with a score of zero would be a
lie that clients then have to detect. `202` plus a `poll_url` says exactly what
is true: accepted, not finished.

### 404 instead of 403 for someone else's resource

Fetching another user's submission or room returns `404`. `403` confirms the id
is real, which turns id enumeration into a discovery tool. The information leak
is small but free to close.

### One error envelope, always

```json
{
  "error": { "code": "language_not_allowed",
             "message": "This question accepts: python, javascript",
             "field": "language" },
  "request_id": "9f2c1a…"
}
```

Validation errors, `HTTPException`s and unhandled exceptions all serialise to
this. Clients never branch on which layer failed. `code` is stable and
machine-readable; `message` is for humans; `request_id` is echoed in the
response header and appears on every log line for that request, so a user can
paste it into a bug report and it resolves to the exact causal chain.

Internal messages are suppressed outside debug: stack traces and driver errors
are reconnaissance.

### Idempotency-Key on submissions

A network timeout on `POST /submissions` leaves the client not knowing whether
the submission was created. Without a key the safe action is to give up; with
one, retrying is free. The pre-check races and the unique index does not, so
the `IntegrityError` path returns the winning submission rather than creating a
second.

### `limit + 1` instead of `COUNT(*)`

Listings fetch one extra row and report `has_more`. Counting a filtered set is
the expensive half of a listing query, and almost every UI only needs to know
whether a next page exists.

### The answer key has no serialisable shape

`TestCaseOut` cannot represent a hidden case. There is no variant of it that
carries one, and `to_detail()` is the only path by which a question reaches a
response model. Making the leak unrepresentable is stronger than remembering to
filter at each call site.

Question payloads are filtered by an **allow-list**. With a deny-list, a new
field is exposed by default, and the first time that happens it is an answer
leak.

### WebSockets are not under `/api/v1`

The protocol is negotiated on the frame, not the path. Versioning the URL would
imply the message format follows the REST version, and it does not.

## Pagination, filtering, validation

Offset pagination with `limit` capped at 100. Keyset would be better for deep
pages; nothing here has deep pages, and the envelope can carry a cursor later
without breaking clients.

Validation happens at the boundary via Pydantic, so the worker never receives a
question it cannot grade, which would otherwise surface as a failed submission
the candidate gets blamed for.

## Versioning

`/api/v1` exists from the start because adding a version later means either
breaking clients or serving two shapes from one path. It costs one path segment
now and buys the option of `v2` alongside `v1`.
