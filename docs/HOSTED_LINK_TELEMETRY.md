# Hosted Link — UX Telemetry Schema

**Issue:** #61 · **Client code:** [`frontend-next/src/telemetry.ts`](../frontend-next/src/telemetry.ts)

Structured events emitted by the hosted `/link` page for UX analytics and
funnel analysis. Telemetry rides on the same channels as the product events
(`OPEN`, `CONNECTED`, `ERROR`, `EXIT`, …), under the event name `TELEMETRY`,
so embedders can tell analytics apart from product outcomes.

## Wire format

Every telemetry message is a product-style envelope whose `event` is always
`TELEMETRY`; which telemetry event it is lives in `name`:

```json
{ "event": "TELEMETRY", "name": "step_view", "elapsed_ms": 1840, "step": "credentials" }
```

(`name` used to be called `event`, which clashed with the envelope; the
envelope is now written last, so a payload can never rename it.)

## Delivery

- **Page → server:** `POST /link/sessions/{link_token}/event` with the body
  above. The server keeps it in the session's event list and fans it out to
  `GET /link/events/{link_token}` (server-sent events) subscribers as an event
  named `TELEMETRY`; its JSON data is `{event, event_id, timestamp, data}`
  with the payload in `data`. Telemetry does not trigger webhooks or change
  the session's status.
- **Page → embedder:** the same payload, plus `source: "plaidify-link"`, goes
  to the embedding window by `postMessage` (only to the session's allowed
  origins), to React Native (`window.ReactNativeWebView.postMessage`), to iOS
  (`window.webkit.messageHandlers.plaidifyLink`) and to Android
  (`window.plaidifyLink.postMessage`).
- **Filtering:** embedders that only want product events drop messages where
  `event === "TELEMETRY"`; analytics consumers keep exactly those and switch
  on `name`.

## Event Schema

Every payload includes:

| Field        | Type   | Notes                                                 |
|--------------|--------|-------------------------------------------------------|
| `event`      | string | Always `"TELEMETRY"`.                                 |
| `name`       | string | One of the telemetry event names below.               |
| `elapsed_ms` | number | Milliseconds since the page's telemetry started (mount). |

Messages to the embedder also carry `source: "plaidify-link"`.

### Events

| `name`                 | Additional fields            | Fires when                                          |
|------------------------|------------------------------|-----------------------------------------------------|
| `step_view`            | `step`                       | A step becomes the active step.                     |
| `step_complete`        | `step`                       | The user moves off a step.                          |
| `field_error`          | `step`, `field`              | Client-side validation rejects a field.             |
| `institution_selected` | `organization_id`            | The user picks an institution.                      |
| `mfa_shown`            | `mfa_type`                   | The MFA prompt is shown (code, push, security question). |
| `mfa_submitted`        | —                            | The user submits an MFA answer.                     |
| `exit_reason`          | `reason`, `error_code?`      | The user leaves Link or the page is torn down.      |

Values:

- `step`: `select`, `credentials`, `connecting`, `mfa`, `success`, `error`.
- `field`: the schema field id (`username`, `password`, `code`, …), never its value.
- `reason`: `user_exit` (the user chose to leave from the error screen), `invalid_link` (no usable session behind the link), `page_closed` (tab closed or webview destroyed).
- `error_code`: a code from the error taxonomy (`GET /link/error-taxonomy`).

## Privacy Posture

Telemetry carries no PII and no credential data. The emitter only accepts the
fields in the schema above, so usernames, passwords, MFA codes, tokens and
free-form error messages never enter a telemetry payload. Safe identifiers:

- `organization_id` (opaque, non-PII)
- `mfa_type` (e.g. `otp_input`, `email_code`, `security_question`, `push`)
- `step` and `field` ids
- `error_code` from the error taxonomy (#55)

As defense in depth, the server strips credential-bearing keys from every
hosted-link event it records or relays (`_sanitize_hosted_event_data()` in
`src/routers/link_sessions.py`): `access_token`, `accessToken`, `password`,
`password_encrypted`, `username_encrypted`, `private_key`, `secret`, `result`
and `data`.

## Retention

- Delivered in real time to server-sent-event subscribers.
- Kept only in the link session's event list, which expires with the session
  (10 minutes); the server writes telemetry to no database table.
- Embedders that want durable analytics forward the events they receive to
  their own pipeline.

## Client Reference

See [`frontend-next/src/telemetry.ts`](../frontend-next/src/telemetry.ts) for
the typed emitter, [`frontend-next/src/events.ts`](../frontend-next/src/events.ts)
for the envelope and delivery, and
[`frontend-next/src/telemetry.test.ts`](../frontend-next/src/telemetry.test.ts)
for the PII-free payload contract.
