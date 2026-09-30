/**
 * Link event handling shared by the React and React Native helpers.
 */

import type { PlaidifyLinkEventPayload } from "./types";

/**
 * Keep only the documented, browser-safe fields of a hosted-link event.
 * Anything else the page (or an impostor) put in the message is dropped.
 */
export function sanitizeLinkPayload(data: unknown): PlaidifyLinkEventPayload | null {
  if (!data || typeof data !== "object") {
    return null;
  }

  const payload = data as PlaidifyLinkEventPayload;
  if (payload.source !== "plaidify-link") {
    return null;
  }

  return {
    source: "plaidify-link",
    event: payload.event,
    error: payload.error,
    error_code: payload.error_code,
    job_id: payload.job_id,
    mfa_type: payload.mfa_type,
    organization_id: payload.organization_id,
    organization_name: payload.organization_name,
    public_token: payload.public_token,
    reason: payload.reason,
    session_id: payload.session_id,
    site: payload.site,
    name: payload.name,
    step: payload.step,
    field: payload.field,
    elapsed_ms: payload.elapsed_ms,
  };
}

/**
 * Events after which Link is finished and its container should close:
 * a connection, or the user leaving. ERROR is not one of them — the page
 * shows retry and choose-another-provider screens after it.
 */
const TERMINAL_EVENTS = new Set(["CONNECTED", "EXIT", "DONE", "CLOSE"]);

export function isTerminalLinkEvent(eventName?: string): boolean {
  return TERMINAL_EVENTS.has(String(eventName || ""));
}
