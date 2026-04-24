/**
 * Typed state machine for the Plaidify hosted-link flow.
 *
 * The legacy vanilla-JS implementation lives in `frontend/link-page.js`
 * and branches on string state names sprinkled across imperative code.
 * This module captures the same flow as a pure, exhaustive reducer so
 * the React rewrite (#51) can render deterministically from state.
 *
 * The five user-visible steps match the DOM regions that the Playwright
 * E2E suite targets today:
 *   - `step-select`        -> institution picker
 *   - `step-credentials`   -> credential entry
 *   - `step-mfa`           -> MFA prompt
 *   - `step-success`       -> completion handoff
 *   - `step-error`         -> recoverable failure
 */

export type FlowStep =
  | "select"
  | "credentials"
  | "connecting"
  | "mfa"
  | "success"
  | "error";

export interface Institution {
  readonly site: string;
  readonly name: string;
  readonly category?: string;
  readonly country?: string;
  readonly logo_url?: string;
  readonly primary_color?: string;
  readonly secondary_color?: string;
  readonly accent_color?: string;
  readonly hint_copy?: string;
  readonly auth_style?: "username_password" | "email_password" | "member_number";
}

export interface SuccessPayload {
  readonly accessToken: string;
  readonly summary?: string;
}

export interface ErrorPayload {
  readonly message: string;
  readonly code?: string;
}

export interface FlowState {
  readonly step: FlowStep;
  readonly institution: Institution | null;
  readonly mfaPrompt: string | null;
  readonly success: SuccessPayload | null;
  readonly error: ErrorPayload | null;
}

export type FlowEvent =
  | { type: "RESET" }
  | { type: "SELECT_INSTITUTION"; institution: Institution }
  | { type: "BACK_TO_PICKER" }
  | { type: "SUBMIT_CREDENTIALS" }
  | { type: "MFA_REQUIRED"; prompt: string }
  | { type: "SUBMIT_MFA" }
  | { type: "SUCCEED"; payload: SuccessPayload }
  | { type: "FAIL"; payload: ErrorPayload };

export const initialFlowState: FlowState = {
  step: "select",
  institution: null,
  mfaPrompt: null,
  success: null,
  error: null,
};

/**
 * Pure reducer over the hosted-link flow. Unknown events leave the
 * state unchanged so callers never have to guard against stale events.
 */
export function flowReducer(state: FlowState, event: FlowEvent): FlowState {
  switch (event.type) {
    case "RESET":
      return initialFlowState;

    case "SELECT_INSTITUTION":
      return {
        ...initialFlowState,
        step: "credentials",
        institution: event.institution,
      };

    case "BACK_TO_PICKER":
      return initialFlowState;

    case "SUBMIT_CREDENTIALS":
      if (state.step !== "credentials" && state.step !== "error") {
        return state;
      }
      return {
        ...state,
        step: "connecting",
        error: null,
      };

    case "MFA_REQUIRED":
      return {
        ...state,
        step: "mfa",
        mfaPrompt: event.prompt,
        error: null,
      };

    case "SUBMIT_MFA":
      if (state.step !== "mfa") {
        return state;
      }
      return {
        ...state,
        step: "connecting",
        error: null,
      };

    case "SUCCEED":
      return {
        ...state,
        step: "success",
        success: event.payload,
        error: null,
      };

    case "FAIL":
      return {
        ...state,
        step: "error",
        error: event.payload,
      };

    default: {
      const exhaustive: never = event;
      return exhaustive;
    }
  }
}
