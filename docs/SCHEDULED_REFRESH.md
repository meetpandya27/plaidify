# Scheduled Refresh

Plaidify periodically re-runs `connect_to_site` for stored access tokens so
hosted data stays fresh without the integrator polling. This document covers
the public API surface, schedule formats, abuse controls, and the standardized
webhook contract.

Schedules are rows in the `scheduled_refresh_jobs` table. The scheduler runs in
one process at a time under a lease (in `redis-worker` mode, in the access
executor), reads the due rows every `REFRESH_TICK_SECONDS` (30 s) and runs up
to `REFRESH_MAX_CONCURRENCY` (5) refreshes at once; each row is claimed with a
conditional update, so a refresh never runs twice and a restart loses nothing.
Refreshes are unattended: a site that asks for MFA or rejects the stored
credentials disables the schedule instead of retrying (see below).

All endpoints need a user access token (`Authorization: Bearer`), not an API key.

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
  "access_token": "<access_token>",
  "schedule_format": "hourly"
}
```

or

```json
{
  "access_token": "<access_token>",
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

Lists your schedules, tokens masked: `access_token` (prefix), `interval_seconds`,
`schedule_format`, `enabled`, `disabled_reason`, `last_refreshed`,
`next_run_at`, `last_error`, `consecutive_failures`. Administrators see every
tenant's schedules (with `user_id`) at `GET /refresh/admin/jobs`.

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
  `_MAX_CONSECUTIVE_FAILURES` (10), the job is auto-disabled
  (`disabled_reason: "max_failures"`) and a `REFRESH_FAILED` webhook is
  dispatched.
- A refresh that meets an MFA challenge, or whose stored credentials are
  rejected, disables the schedule at once (`disabled_reason: "needs_reauth"`,
  the access job ends as `failed` with `error_code: "mfa_required"` for MFA)
  and sends one `REFRESH_FAILED`: the user has to link again.

## Webhook contract (`event_version: 2`)

Refresh webhooks go to the webhooks registered for the access token's link
(`POST /webhooks/register`), through the same signed, retried outbox as every
other webhook: headers `X-Plaidify-Event`, `X-Plaidify-Delivery`,
`X-Plaidify-Timestamp` and `X-Plaidify-Signature: sha256=<hex HMAC-SHA256 of
"{timestamp}." + raw body>`, and `delivery_id` / `webhook_id` added to the body.

Both refresh-triggered webhook events share a base envelope:

```json
{
  "event_version": 2,
  "access_token_prefix": "3f0946a5-af2...",
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
  "access_token_prefix": "3f0946a5-af2...",
  "timestamp": "2026-04-24T12:00:00+00:00",
  "success": true,
  "fields_updated": ["balance", "transactions"]
}
```

### `REFRESH_FAILED`

Fired once when a schedule is disabled: after `_MAX_CONSECUTIVE_FAILURES`
(10) consecutive failures (`reason: "max_failures"`), or at once when the site
asks for MFA or rejects the credentials (`reason: "needs_reauth"`).

```json
{
  "event": "REFRESH_FAILED",
  "event_version": 2,
  "access_token_prefix": "3f0946a5-af2...",
  "timestamp": "2026-04-24T12:00:00+00:00",
  "success": false,
  "reason": "max_failures",
  "error": "<sanitized exception message>",
  "consecutive_failures": 10
}
```

Integrators should re-enable a disabled schedule via
`PATCH /refresh/schedule/{access_token}` with `{"enabled": true}` after
addressing the underlying issue; for `needs_reauth`, the user links the
account again first.
