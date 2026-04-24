import { describe, expect, it } from "vitest";
import {
  REMEDIATIONS,
  classifyError,
  classifyMessage,
  remediationFor,
} from "./errorTaxonomy";

describe("errorTaxonomy", () => {
  it("ships a remediation for every code", () => {
    const keys = Object.keys(REMEDIATIONS);
    expect(keys).toHaveLength(7);
    for (const remediation of Object.values(REMEDIATIONS)) {
      expect(remediation.title).toBeTruthy();
      expect(remediation.description).toBeTruthy();
      expect(remediation.primary_cta).toBeTruthy();
      expect(remediation.primary_action).toBeTruthy();
    }
  });

  it("remediationFor falls back to internal_error for unknown codes", () => {
    const fallback = remediationFor("does_not_exist" as never);
    expect(fallback).toBe(REMEDIATIONS.internal_error);
  });

  it("remediationFor handles undefined", () => {
    expect(remediationFor(undefined)).toBe(REMEDIATIONS.internal_error);
  });

  it("classifyError reads error_code off the error instance", () => {
    const err = Object.assign(new Error("anything"), {
      error_code: "invalid_credentials",
    });
    expect(classifyError(err)).toBe("invalid_credentials");
  });

  it("classifyError falls back to message heuristics", () => {
    expect(classifyError(new Error("Invalid credentials provided"))).toBe(
      "invalid_credentials",
    );
    expect(classifyError(new Error("Network offline"))).toBe("network_error");
    expect(classifyError(new Error("MFA timeout reached"))).toBe("mfa_timeout");
    expect(classifyError(new Error("Rate limit exceeded"))).toBe("rate_limited");
    expect(classifyError(new Error("Something random"))).toBe("internal_error");
  });

  it("classifyMessage handles empty inputs", () => {
    expect(classifyMessage(undefined)).toBe("internal_error");
    expect(classifyMessage("")).toBe("internal_error");
  });
});
