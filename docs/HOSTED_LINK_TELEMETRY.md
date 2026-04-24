# Hosted Link — UX Telemetry Schema

**Status:** Stable · **Owner:** Link surface team · **Issue:** #61

Structured events emitted by the hosted `/link` UI for UX analytics and
flow funnel analysis. Telemetry rides on the same post-message / SSE
event bus as the product-level bridge events, but under a dedicated
event name (`TELEMETRY`) so embedders can easily distinguish analytics
from product outcomes.

## Delivery

- **Client → server:** `POST /link/sessions/{link_token}/event` with
  body `{ "event": "TELEMETRY", ...payload }`. Sanitized by
  `_sanitize_hosted_event_data()` before fan-out.
- **Server → subscribers:** broadcast via
  `GET /link/events/{link_token}` (SSE) alongside bridge events.
- **Embedder:** receives via `window.postMessage` and native bridges
  (`ReactNativeWebView.postMessage`, WebKit handlers). Embedders that
  only consume product events can filter on `type !== "TELEMETRY"`.

## Event Schema

Every payload includes:

| Field        | Type   | Notes                                                 |
|--------------|--------|-------------------------------------------------------|
| `event`      | string | One of the event names below.                         |
| `elapsed_ms` | number | Milliseconds since telemetry session start (mount).   |

### Events

| Name                   | Additional fields            | Fires when                                          |
|------------------------|------------------------------|-----------------------------------------------------|
| `step_view`            | `step`                       | A step becomes the active step.                     |
| `step_complete`        | `step`                       | The user advances off a step.                       |
| `field_error`          | `step`, `field`              | Client-side validation rejects a field.             |
| `institution_selected` | `organization_id`            | User picks an institution.                          |
| `mfa_shown`            | `mfa_type`                   | MFA prompt is rendered (OTP, push, security Qs).    |
| `mfa_submitted`        | —                            | User submits an MFA response.                       |
| `exit_reason`          | `reason`, `error_code?`      | Link surface unmounts; reason is a short token.     |

## Privacy Posture

Telemetry payloads **must never contain PII** or credential-bearing
data. The following fields are explicitly forbidden in any telemetry
event and are stripped server-side by `_sanitize_hosted_event_data()`:

- `username`, `password`, `code`, `otp`
- `public_token`, `access_token`, `session_id` (bridge-only)
- `encrypted`, raw `message` bodies
- Free-form error messages (use taxonomy `error_code` instead)

Identifiers that are safe to include:

- `organization_id` (opaque, non-PII)
- `mfa_type` (e.g. `otp`, `push`, `security_questions`)
- `step` id (`consent`, `picker`, `credentials`, `connecting`, `mfa`, `success`, `error`)
- `field` id (schema field name, never its value)
- `error_code` from the error taxonomy (#55)

## Retention

Telemetry events share the lifecycle of the hosted link session:

- Delivered in real time to SSE subscribers.
- Not persisted beyond session close — the server does not store
  telemetry in long-term tables.
- Embedders that want durable analytics are expected to forward
  received events to their own analytics pipeline.

## Client Reference

See [`frontend-next/src/telemetry.ts`](../frontend-next/src/telemetry.ts)
for the typed emitter and
[`frontend-next/src/telemetry.test.ts`](../frontend-next/src/telemetry.test.ts)
for the PII-free payload contract.
