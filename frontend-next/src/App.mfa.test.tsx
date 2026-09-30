/**
 * Drives the hosted page through a real MFA journey: provider → credentials
 * → code → success. The SSR smoke tests only render single steps, which is
 * how an unhandled "mfa_submitted" reply shipped as an error screen.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { App } from "./App";
import {
  pollLinkSession,
  type ConnectResponse,
  type LinkApi,
  type LinkSessionStatus,
  type Organization,
} from "./api";
import type { EventDelivery } from "./events";

const utility: Organization = {
  organization_id: "org-greengrid",
  site: "demo_utility",
  name: "GreenGrid Energy",
  auth_style: "username_password",
};

function buildApi() {
  let submitted = false;
  let pollsAfterSubmit = 0;
  return {
    getStatus: vi.fn(async (): Promise<LinkSessionStatus> => {
      if (!submitted) {
        return { status: "awaiting_credentials", site: "demo_utility" };
      }
      pollsAfterSubmit += 1;
      // The challenge just answered keeps reading "mfa_required" until the
      // job moves on; the page must not prompt for it again.
      if (pollsAfterSubmit === 1) {
        return { status: "mfa_required", session_id: "access-1", mfa_type: "otp_input" };
      }
      return { status: "completed", session_id: "access-1", public_token: "public-xyz" };
    }),
    searchOrganizations: vi.fn(async () => ({ results: [utility] })),
    getEncryptionPublicKey: vi.fn(async () => ({ public_key: "pem" })),
    connect: vi.fn(
      async (): Promise<ConnectResponse> => ({
        status: "mfa_required",
        session_id: "access-1",
        mfa_type: "otp_input",
        metadata: { message: "Enter the code we sent you" },
      }),
    ),
    submitMfa: vi.fn(async (): Promise<ConnectResponse> => {
      submitted = true;
      return { status: "mfa_submitted" };
    }),
  };
}

describe("App MFA journey", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("completes after the code is accepted instead of failing on mfa_submitted", async () => {
    window.history.replaceState(null, "", "/link?token=tok-mfa");
    const api = buildApi();
    const enqueue = vi.fn();

    render(
      <App
        seedInstitutions={[utility]}
        apiFactory={() => api as unknown as LinkApi}
        encryptCredentials={async () => ({ username: "u-enc", password: "p-enc" })}
        pollLinkSession={(options) => pollLinkSession({ ...options, sleep: async () => {} })}
        buildEventDelivery={() =>
          ({ enqueue, flushOnTeardown: () => {}, dispose: () => {} }) as unknown as EventDelivery
        }
      />,
    );

    fireEvent.click(await screen.findByText("GreenGrid Energy"));
    fireEvent.change(document.getElementById("link-username")!, { target: { value: "demo_mfa" } });
    fireEvent.change(document.getElementById("link-password")!, { target: { value: "demo_pass" } });
    fireEvent.click(document.getElementById("connect-btn")!);

    await waitFor(() => expect(document.getElementById("step-mfa")).toHaveClass("active"));
    fireEvent.change(document.getElementById("mfa-code")!, { target: { value: "123456" } });
    fireEvent.click(document.getElementById("mfa-submit-btn")!);

    await waitFor(() => expect(document.getElementById("step-success")).toHaveClass("active"));
    expect(api.submitMfa).toHaveBeenCalledWith({ sessionId: "access-1", code: "123456" });
    // The token goes to the embedding app, never onto the screen.
    expect(screen.queryByText("public-xyz")).toBeNull();
    expect(enqueue).toHaveBeenCalledWith(
      "CONNECTED",
      expect.objectContaining({ public_token: "public-xyz" }),
    );
    expect(enqueue).not.toHaveBeenCalledWith("ERROR", expect.anything());
  });
});

describe("App MFA retry after a wrong code", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("asks again with the attempts left, ignores the stale prompt, then completes", async () => {
    window.history.replaceState(null, "", "/link?token=tok-retry");
    let submits = 0;
    let pollsSinceSubmit = 0;
    const rejected = {
      status: "mfa_required",
      session_id: "access-1",
      mfa_type: "otp_input",
      metadata: { mfa_error: "invalid_code", attempts_remaining: 2 },
    } as const;
    const api = {
      getStatus: vi.fn(async (): Promise<LinkSessionStatus> => {
        if (submits === 0) return { status: "awaiting_credentials", site: "demo_utility" };
        pollsSinceSubmit += 1;
        if (submits === 1) {
          // Right after the first answer the challenge still reads as open,
          // then the site's rejection re-opens it with fewer attempts left.
          return pollsSinceSubmit === 1 ? { status: "mfa_required", session_id: "access-1" } : rejected;
        }
        // Right after the second answer the rejection is still on the session
        // for a beat; it must not be shown as a new prompt.
        return pollsSinceSubmit === 1
          ? rejected
          : { status: "completed", session_id: "access-1", public_token: "public-ok" };
      }),
      searchOrganizations: vi.fn(async () => ({ results: [utility] })),
      getEncryptionPublicKey: vi.fn(async () => ({ public_key: "pem" })),
      connect: vi.fn(
        async (): Promise<ConnectResponse> => ({
          status: "mfa_required",
          session_id: "access-1",
          mfa_type: "otp_input",
          metadata: { message: "Enter the code we sent you" },
        }),
      ),
      submitMfa: vi.fn(async (): Promise<ConnectResponse> => {
        submits += 1;
        pollsSinceSubmit = 0;
        return { status: "mfa_submitted" };
      }),
    };
    const enqueue = vi.fn();

    render(
      <App
        seedInstitutions={[utility]}
        apiFactory={() => api as unknown as LinkApi}
        encryptCredentials={async () => ({ username: "u-enc", password: "p-enc" })}
        pollLinkSession={(options) => pollLinkSession({ ...options, sleep: async () => {} })}
        buildEventDelivery={() =>
          ({ enqueue, flushOnTeardown: () => {}, dispose: () => {} }) as unknown as EventDelivery
        }
      />,
    );

    fireEvent.click(await screen.findByText("GreenGrid Energy"));
    fireEvent.change(document.getElementById("link-username")!, { target: { value: "demo_mfa" } });
    fireEvent.change(document.getElementById("link-password")!, { target: { value: "demo_pass" } });
    fireEvent.click(document.getElementById("connect-btn")!);

    await waitFor(() => expect(document.getElementById("step-mfa")).toHaveClass("active"));
    fireEvent.change(document.getElementById("mfa-code")!, { target: { value: "000000" } });
    fireEvent.click(document.getElementById("mfa-submit-btn")!);

    expect(await screen.findByText(/That code didn't work\. Check it and try again\. Attempts left: 2/)).toBeInTheDocument();
    expect(document.getElementById("step-mfa")).toHaveClass("active");

    fireEvent.change(document.getElementById("mfa-code")!, { target: { value: "123456" } });
    fireEvent.click(document.getElementById("mfa-submit-btn")!);

    await waitFor(() => expect(document.getElementById("step-success")).toHaveClass("active"));
    expect(api.submitMfa).toHaveBeenCalledTimes(2);
    expect(enqueue).toHaveBeenCalledWith("CONNECTED", expect.objectContaining({ public_token: "public-ok" }));
  });
});
