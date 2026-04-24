import { describe, expect, it } from "vitest";

import {
  flowReducer,
  initialFlowState,
  type FlowState,
  type Institution,
} from "./state";

const hydro: Institution = {
  site: "hydro_one",
  name: "Hydro One",
  category: "utilities",
  country: "CA",
};

describe("flowReducer", () => {
  it("starts at the institution picker", () => {
    expect(initialFlowState.step).toBe("select");
    expect(initialFlowState.institution).toBeNull();
  });

  it("moves to credentials when an institution is selected", () => {
    const next = flowReducer(initialFlowState, {
      type: "SELECT_INSTITUTION",
      institution: hydro,
    });
    expect(next.step).toBe("credentials");
    expect(next.institution).toEqual(hydro);
  });

  it("moves back to the picker from credentials", () => {
    const credentials = flowReducer(initialFlowState, {
      type: "SELECT_INSTITUTION",
      institution: hydro,
    });
    const back = flowReducer(credentials, { type: "BACK_TO_PICKER" });
    expect(back).toEqual(initialFlowState);
  });

  it("only advances to connecting from credentials or error", () => {
    const picker: FlowState = initialFlowState;
    expect(flowReducer(picker, { type: "SUBMIT_CREDENTIALS" })).toBe(picker);

    const credentials = flowReducer(picker, {
      type: "SELECT_INSTITUTION",
      institution: hydro,
    });
    const connecting = flowReducer(credentials, { type: "SUBMIT_CREDENTIALS" });
    expect(connecting.step).toBe("connecting");
    expect(connecting.error).toBeNull();

    const errored = flowReducer(connecting, {
      type: "FAIL",
      payload: { message: "bad password" },
    });
    expect(errored.step).toBe("error");

    const retried = flowReducer(errored, { type: "SUBMIT_CREDENTIALS" });
    expect(retried.step).toBe("connecting");
    expect(retried.error).toBeNull();
  });

  it("captures the MFA prompt and only allows submit from the MFA step", () => {
    const connecting = flowReducer(
      flowReducer(initialFlowState, {
        type: "SELECT_INSTITUTION",
        institution: hydro,
      }),
      { type: "SUBMIT_CREDENTIALS" },
    );

    const mfa = flowReducer(connecting, {
      type: "MFA_REQUIRED",
      prompt: "Enter the 6-digit code",
    });
    expect(mfa.step).toBe("mfa");
    expect(mfa.mfaPrompt).toBe("Enter the 6-digit code");

    const ignored = flowReducer(initialFlowState, { type: "SUBMIT_MFA" });
    expect(ignored).toBe(initialFlowState);

    const verifying = flowReducer(mfa, { type: "SUBMIT_MFA" });
    expect(verifying.step).toBe("connecting");
  });

  it("records success and resets cleanly", () => {
    const connecting = flowReducer(
      flowReducer(initialFlowState, {
        type: "SELECT_INSTITUTION",
        institution: hydro,
      }),
      { type: "SUBMIT_CREDENTIALS" },
    );

    const success = flowReducer(connecting, {
      type: "SUCCEED",
      payload: { accessToken: "tok_abc", summary: "Account linked." },
    });
    expect(success.step).toBe("success");
    expect(success.success?.accessToken).toBe("tok_abc");

    const reset = flowReducer(success, { type: "RESET" });
    expect(reset).toEqual(initialFlowState);
  });
});
