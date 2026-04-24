/**
 * Mirror of `src/error_taxonomy.py` for the hosted-link frontend
 * (issue #55). The backend also exposes this at `/link/error-taxonomy`;
 * we ship a static copy so the UI can render remediation screens
 * before the first network round-trip.
 *
 * Keep in sync with the Python source of truth.
 */

export type LinkErrorCode =
  | "invalid_credentials"
  | "mfa_timeout"
  | "institution_down"
  | "rate_limited"
  | "unsupported_site"
  | "network_error"
  | "internal_error";

export type RemediationAction =
  | "retry"
  | "back_to_picker"
  | "exit"
  | "contact_support";

export interface Remediation {
  readonly title: string;
  readonly description: string;
  readonly primary_cta: string;
  readonly primary_action: RemediationAction;
  readonly secondary_cta: string | null;
  readonly secondary_action: RemediationAction | null;
  readonly retryable: boolean;
}

export const REMEDIATIONS: Readonly<Record<LinkErrorCode, Remediation>> = {
  invalid_credentials: {
    title: "We couldn't sign you in",
    description:
      "The username or password doesn't match what your provider has on file. Double-check your credentials and try again, or reset them with your provider.",
    primary_cta: "Try again",
    primary_action: "retry",
    secondary_cta: "Choose a different provider",
    secondary_action: "back_to_picker",
    retryable: true,
  },
  mfa_timeout: {
    title: "Verification timed out",
    description:
      "We didn't receive your verification code in time. You can request a new code and try again.",
    primary_cta: "Start over",
    primary_action: "retry",
    secondary_cta: "Choose a different provider",
    secondary_action: "back_to_picker",
    retryable: true,
  },
  institution_down: {
    title: "Your provider is temporarily unavailable",
    description:
      "Their systems aren't responding right now. This usually resolves within a few minutes — try again shortly, or pick a different provider.",
    primary_cta: "Try again",
    primary_action: "retry",
    secondary_cta: "Choose a different provider",
    secondary_action: "back_to_picker",
    retryable: true,
  },
  rate_limited: {
    title: "Too many attempts",
    description:
      "We're pausing briefly to protect your account. Please wait a moment before trying again.",
    primary_cta: "Try again in a moment",
    primary_action: "retry",
    secondary_cta: "Exit",
    secondary_action: "exit",
    retryable: true,
  },
  unsupported_site: {
    title: "Provider not supported yet",
    description:
      "We don't have an integration for this provider. Pick a different one, or let us know what you'd like us to add.",
    primary_cta: "Choose a different provider",
    primary_action: "back_to_picker",
    secondary_cta: "Contact support",
    secondary_action: "contact_support",
    retryable: false,
  },
  network_error: {
    title: "Connection interrupted",
    description:
      "We lost the connection to your provider. Check your internet connection and try again.",
    primary_cta: "Try again",
    primary_action: "retry",
    secondary_cta: "Choose a different provider",
    secondary_action: "back_to_picker",
    retryable: true,
  },
  internal_error: {
    title: "Something went wrong on our end",
    description:
      "An unexpected error occurred while setting up your secure connection. Please try again, and contact support if this keeps happening.",
    primary_cta: "Try again",
    primary_action: "retry",
    secondary_cta: "Contact support",
    secondary_action: "contact_support",
    retryable: true,
  },
};

export function remediationFor(code: LinkErrorCode | string | null | undefined): Remediation {
  if (code && code in REMEDIATIONS) {
    return REMEDIATIONS[code as LinkErrorCode];
  }
  return REMEDIATIONS.internal_error;
}

/**
 * Best-effort classification of a caught Error into a structured code.
 * Prefers an explicit code embedded on the error (e.g. from a fetch
 * response body), then falls back to simple heuristics.
 */
export function classifyError(error: unknown): LinkErrorCode {
  if (error && typeof error === "object") {
    const maybeCode = (error as { error_code?: unknown }).error_code;
    if (typeof maybeCode === "string" && maybeCode in REMEDIATIONS) {
      return maybeCode as LinkErrorCode;
    }
    const message = (error as { message?: unknown }).message;
    if (typeof message === "string") {
      return classifyMessage(message);
    }
  }
  if (typeof error === "string") {
    return classifyMessage(error);
  }
  return "internal_error";
}

export function classifyMessage(message: string | null | undefined): LinkErrorCode {
  if (!message) {
    return "internal_error";
  }
  const lower = message.toLowerCase();
  if (lower.includes("credential") || lower.includes("authentication")) {
    return "invalid_credentials";
  }
  if (lower.includes("mfa") && lower.includes("timeout")) {
    return "mfa_timeout";
  }
  if (lower.includes("rate") && lower.includes("limit")) {
    return "rate_limited";
  }
  if (lower.includes("unsupported") || lower.includes("not supported")) {
    return "unsupported_site";
  }
  if (lower.includes("network") || lower.includes("offline") || lower.includes("timeout")) {
    return "network_error";
  }
  if (lower.includes("unavailable") || lower.includes("down")) {
    return "institution_down";
  }
  return "internal_error";
}
