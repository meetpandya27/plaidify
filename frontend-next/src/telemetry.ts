/**
 * Structured UX telemetry for hosted /link (issue #61).
 *
 * Every telemetry event is dispatched through the same
 * {@link EventDelivery} pipeline as the product-level bridge events,
 * but under a separate event name (`TELEMETRY`) so:
 *
 *   1. Embedders that only care about product outcomes
 *      (`CONNECTED`, `ERROR`, `EXIT`) can filter telemetry out.
 *   2. UX analytics can subscribe to the `/link/events/` SSE stream
 *      and aggregate flow funnels without touching the product bus.
 *
 * ## Privacy
 * Telemetry payloads MUST NOT contain PII. Usernames, passwords,
 * MFA codes, public tokens, and any other user-supplied credential
 * data are filtered at the call site. The backend enforces the same
 * contract in `_sanitize_hosted_event_data()`.
 *
 * ## Retention
 * Telemetry events inherit the hosting session's retention (TTL on
 * the link token). They are not persisted beyond session close.
 */

export type TelemetryEventName =
  | "step_view"
  | "step_complete"
  | "field_error"
  | "institution_selected"
  | "mfa_shown"
  | "mfa_submitted"
  | "exit_reason";

export interface TelemetryPayload {
  readonly event: TelemetryEventName;
  /** Milliseconds since the telemetry session began. */
  readonly elapsed_ms: number;
  /** Current step identifier at event time. */
  readonly step?: string;
  /** For field_error: the field id that failed validation. */
  readonly field?: string;
  /** For institution_selected: organization id. PII-free. */
  readonly organization_id?: string;
  /** For mfa_shown: the prompt type (otp/push/etc), never the code. */
  readonly mfa_type?: string;
  /** For exit_reason: a short machine-readable reason. */
  readonly reason?: string;
  /** Error code from the taxonomy when relevant (optional). */
  readonly error_code?: string;
}

export interface TelemetryEmitter {
  /** Emit an event into the same post-message/SSE bus used for bridge events. */
  readonly emit: (event: string, payload: Record<string, unknown>) => void;
}

/**
 * Build a telemetry helper bound to a specific emitter and start
 * timestamp. The returned object exposes one method per event name
 * so call sites don't stringly-type the event identifier.
 */
export function createTelemetry(emitter: TelemetryEmitter, now: () => number = Date.now) {
  const start = now();
  const send = (event: TelemetryEventName, extra: Omit<TelemetryPayload, "event" | "elapsed_ms"> = {}) => {
    const payload: TelemetryPayload = {
      event,
      elapsed_ms: Math.max(0, now() - start),
      ...extra,
    };
    emitter.emit("TELEMETRY", payload as unknown as Record<string, unknown>);
  };
  return {
    stepView: (step: string) => send("step_view", { step }),
    stepComplete: (step: string) => send("step_complete", { step }),
    fieldError: (step: string, field: string) => send("field_error", { step, field }),
    institutionSelected: (organizationId: string) =>
      send("institution_selected", { organization_id: organizationId }),
    mfaShown: (mfaType: string) => send("mfa_shown", { mfa_type: mfaType }),
    mfaSubmitted: () => send("mfa_submitted", {}),
    exitReason: (reason: string, errorCode?: string) =>
      send("exit_reason", { reason, error_code: errorCode }),
  };
}

export type Telemetry = ReturnType<typeof createTelemetry>;
