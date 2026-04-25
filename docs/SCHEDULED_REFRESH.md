# Scheduled Refresh

Plaidify periodically re-runs `connect_to_site` for stored access tokens so
hosted data stays fresh without the integrator polling. This document covers
the public API surface, schedule formats, abuse controls, and the standardized
webhook contract.

## Schedule formats

The scheduler accepts four formats:

| `schedule_format` | `interval_seconds` (effective) | Notes |
|---|---|---|
| `interval` (default) | caller-supplied | Minimum 300 s (5 min) at the API. |
| `hourly`             | 3 600                           | Preset; ignores any supplied interval. |
| `daily`              | 86 400                          | Preset. |
| `weekly`             | 604 800                         | Preset. |

Future formats (e.g. `cron`) can be added without breaking the schema —
clients should treat unknown values as opaque strings.

## Endpoints

### `POST /refresh/schedule`

Body:

```json
{
  "access_token": "acc-…",
  "schedule_format": "hourly"
}
```

or

```json
{
  "access_token": "acc-…",
  "schedule_format": "interval",
  "interval_seconds": 1800
}
```

### `PATCH /refresh/schedule/{access_token}`

Update any combination of `interval_seconds`, `schedule_format`, `enabled`.
Returns the post-update schedule. Re-enabling resets `consecutive_failures`.

### `DELETE /refresh/schedule/{access_token}`

Removes the schedule.

### `GET /refresh/jobs`

Lists active schedules for the authenticated user (admin scope today).

### `POST /create_link` — deferred binding

`/create_link` accepts an optional `refresh_schedule` body field that is
stashed against the link token and applied once `/submit_credentials`
mints an access token. This lets integrators express "create + schedule"
in one round trip.

```json
{
  "scopes": ["balance"],
  "refresh_schedule": { "schedule_format": "daily" }
}
```

The directive is validated at `/create_link` time (bad formats / sub-minimum
intervals return `400`), then consumed exactly once at `/submit_credentials`.

## Abuse controls

- `POST /refresh/schedule` and `PATCH /refresh/schedule/{access_token}` are
  rate-limited to **30 requests / minute / client** via `slowapi`.
- A user may have at most **`MAX_SCHEDULES_PER_USER` (default 50)**
  active schedules. Attempting to register a 51st returns `429`.
- Per-job exponential backoff doubles the effective interval on each
  consecutive failure, capped at 24 h. After
  `_MAX_CONSECUTIVE_FAILURES` (10), the job is auto-disabled and a
  `REFRESH_FAILED` webhook is dispatched.

## Webhook contract (`event_version: 2`)

Both refresh-triggered webhook events share a base envelope:

```json
{
  "event_version": 2,
  "access_token_prefix": "acc-abc1234...",
  "timestamp": "2026-04-24T12:00:00+00:00",
  "event": "DATA_REFRESHED" | "REFRESH_FAILED",
  "success": true | false
}
```

### `DATA_REFRESHED`

```json
{
  "event": "DATA_REFRESHED",
  "event_version": 2,
  "access_token_prefix": "acc-abc1234...",
  "timestamp": "2026-04-24T12:00:00+00:00",
  "success": true,
  "fields_updated": ["balance", "transactions"]
}
```

### `REFRESH_FAILED`

Fired exactly once when a job is auto-disabled after
`_MAX_CONSECUTIVE_FAILURES` (10) consecutive failures.

```json
{
  "event": "REFRESH_FAILED",
  "event_version": 2,
  "access_token_prefix": "acc-abc1234...",
  "timestamp": "2026-04-24T12:00:00+00:00",
  "success": false,
  "error": "<sanitized exception message>",
  "consecutive_failures": 10
}
```

Integrators should re-enable a disabled schedule via
`PATCH /refresh/schedule/{access_token}` with `{"enabled": true}` after
addressing the underlying issue (typically expired credentials).
