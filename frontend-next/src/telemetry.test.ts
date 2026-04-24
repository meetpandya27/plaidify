import { describe, expect, it, vi } from "vitest";

import { createTelemetry } from "./telemetry";

describe("telemetry", () => {
  const makeHarness = () => {
    const events: Array<{ event: string; payload: Record<string, unknown> }> = [];
    let clock = 1_000;
    const now = () => clock;
    const advance = (ms: number) => {
      clock += ms;
    };
    const emitter = {
      emit: (event: string, payload: Record<string, unknown>) => {
        events.push({ event, payload });
      },
    };
    const telemetry = createTelemetry(emitter, now);
    return { events, advance, telemetry };
  };

  it("emits step_view with elapsed_ms since session start", () => {
    const { events, advance, telemetry } = makeHarness();
    advance(250);
    telemetry.stepView("picker");
    expect(events).toHaveLength(1);
    expect(events[0].event).toBe("TELEMETRY");
    expect(events[0].payload).toMatchObject({
      event: "step_view",
      step: "picker",
      elapsed_ms: 250,
    });
  });

  it("emits step_complete with the step id", () => {
    const { events, telemetry } = makeHarness();
    telemetry.stepComplete("credentials");
    expect(events[0].payload).toMatchObject({
      event: "step_complete",
      step: "credentials",
    });
  });

  it("emits field_error with step + field identifiers (no values)", () => {
    const { events, telemetry } = makeHarness();
    telemetry.fieldError("credentials", "username");
    const payload = events[0].payload as Record<string, unknown>;
    expect(payload.event).toBe("field_error");
    expect(payload.step).toBe("credentials");
    expect(payload.field).toBe("username");
    // No value/PII field should ever be present.
    expect(payload).not.toHaveProperty("value");
    expect(payload).not.toHaveProperty("username");
  });

  it("emits institution_selected with organization_id only", () => {
    const { events, telemetry } = makeHarness();
    telemetry.institutionSelected("org_rbc");
    const payload = events[0].payload as Record<string, unknown>;
    expect(payload.event).toBe("institution_selected");
    expect(payload.organization_id).toBe("org_rbc");
    expect(payload).not.toHaveProperty("organization_name");
  });

  it("emits mfa_shown with prompt type but never the code", () => {
    const { events, telemetry } = makeHarness();
    telemetry.mfaShown("otp");
    const payload = events[0].payload as Record<string, unknown>;
    expect(payload.event).toBe("mfa_shown");
    expect(payload.mfa_type).toBe("otp");
    expect(payload).not.toHaveProperty("code");
    expect(payload).not.toHaveProperty("value");
  });

  it("emits mfa_submitted without any credential body", () => {
    const { events, telemetry } = makeHarness();
    telemetry.mfaSubmitted();
    const payload = events[0].payload as Record<string, unknown>;
    expect(payload.event).toBe("mfa_submitted");
    expect(Object.keys(payload).sort()).toEqual(["elapsed_ms", "event"].sort());
  });

  it("emits exit_reason with reason + optional error_code", () => {
    const { events, telemetry } = makeHarness();
    telemetry.exitReason("unmount", "rate_limited");
    const payload = events[0].payload as Record<string, unknown>;
    expect(payload.event).toBe("exit_reason");
    expect(payload.reason).toBe("unmount");
    expect(payload.error_code).toBe("rate_limited");
  });

  it("default clock uses Date.now when now() is not supplied", () => {
    const spy = vi.spyOn(Date, "now").mockReturnValue(42);
    const events: Array<{ event: string; payload: Record<string, unknown> }> = [];
    const telemetry = createTelemetry({
      emit: (event, payload) => {
        events.push({ event, payload });
      },
    });
    telemetry.stepView("picker");
    expect((events[0].payload as { elapsed_ms: number }).elapsed_ms).toBe(0);
    spy.mockRestore();
  });
});
