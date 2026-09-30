/**
 * Drives the hosted page through the journeys the SDKs depend on: when
 * EXIT is (and is not) sent, which MFA form a provider gets, what happens
 * to typed credentials, and where parent-frame events may go.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { App } from "./App";
import {
  ApiError,
  pollLinkSession,
  type ConnectResponse,
  type LinkApi,
  type LinkSessionStatus,
  type MfaSchema,
  type Organization,
} from "./api";
import type { EventDelivery } from "./events";

const mfaSchema: MfaSchema = {
  otp_input: {
    title: "Enter your verification code",
    submit_label: "Verify and continue",
    fields: [
      {
        id: "code",
        label: "Verification code",
        type: "text",
        pattern: "^\\d{4,8}$",
        min_length: 4,
        max_length: 8,
        required: true,
      },
    ],
  },
  security_question: {
    title: "Answer your security question",
    submit_label: "Continue",
    fields: [{ id: "code", label: "Answer", type: "text", required: true, max_length: 128 }],
  },
  push: {
    title: "Approve the push notification on your device",
    submit_label: "I approved it",
    fields: [],
  },
};

const bank: Organization = {
  organization_id: "org-bank",
  site: "hydro_one",
  name: "Anchor Point Bank",
  auth_style: "username_password",
  mfa_schema: mfaSchema,
};

const utility: Organization = {
  organization_id: "org-utility",
  site: "demo_utility",
  name: "GreenGrid Energy",
  auth_style: "username_password",
};

interface ApiOverrides {
  getStatus?: () => Promise<LinkSessionStatus>;
  connect?: () => Promise<ConnectResponse>;
  submitMfa?: () => Promise<ConnectResponse>;
  searchOrganizations?: (params: { site?: string }) => Promise<{ results: Organization[] }>;
}

function buildApi(overrides: ApiOverrides = {}) {
  return {
    getStatus: vi.fn(overrides.getStatus ?? (async () => ({ status: "awaiting_institution" }))),
    searchOrganizations: vi.fn(overrides.searchOrganizations ?? (async () => ({ results: [bank, utility] }))),
    getEncryptionPublicKey: vi.fn(async () => ({ public_key: "pem" })),
    connect: vi.fn(overrides.connect ?? (async (): Promise<ConnectResponse> => ({ status: "connected" }))),
    submitMfa: vi.fn(overrides.submitMfa ?? (async (): Promise<ConnectResponse> => ({ status: "mfa_submitted" }))),
  };
}

function buildDelivery() {
  return {
    enqueue: vi.fn(),
    flushOnTeardown: vi.fn(),
    dispose: vi.fn(),
  };
}

type Api = ReturnType<typeof buildApi>;
type Delivery = ReturnType<typeof buildDelivery>;

function renderApp(
  api: Api,
  delivery: Delivery,
  extra: Partial<Parameters<typeof App>[0]> = {},
) {
  return render(
    <App
      seedInstitutions={[bank, utility]}
      apiFactory={() => api as unknown as LinkApi}
      encryptCredentials={async (_pem, username, password) => ({
        username: `enc(${username})`,
        password: `enc(${password})`,
      })}
      pollLinkSession={(options) => pollLinkSession({ ...options, sleep: async () => {} })}
      buildEventDelivery={() => delivery as unknown as EventDelivery}
      {...extra}
    />,
  );
}

function enqueued(delivery: Delivery, event: string) {
  return delivery.enqueue.mock.calls.filter(([name]) => name === event);
}

function input(id: string): HTMLInputElement {
  return document.getElementById(id) as HTMLInputElement;
}

async function signIn(provider: string, username: string, password: string) {
  fireEvent.click(await screen.findByText(provider));
  await waitFor(() => expect(document.getElementById("step-credentials")).toHaveClass("active"));
  fireEvent.change(input("link-username"), { target: { value: username } });
  fireEvent.change(input("link-password"), { target: { value: password } });
  fireEvent.click(document.getElementById("connect-btn")!);
}

describe("App lifecycle events", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("sends no EXIT and drops no events when an attempt fails", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    const api = buildApi({
      connect: async () => {
        throw new ApiError("Invalid credentials", 401, "invalid_credentials");
      },
    });
    const delivery = buildDelivery();
    renderApp(api, delivery);

    await signIn("Anchor Point Bank", "alice", "hunter22");
    await waitFor(() => expect(document.getElementById("step-error")).toHaveClass("active"));

    expect(enqueued(delivery, "ERROR")).toHaveLength(1);
    expect(enqueued(delivery, "EXIT")).toHaveLength(0);
    expect(delivery.dispose).not.toHaveBeenCalled();
    expect(delivery.flushOnTeardown).not.toHaveBeenCalled();
  });

  it("sends EXIT once when the page is torn down, by beacon", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    const api = buildApi();
    const delivery = buildDelivery();
    renderApp(api, delivery);
    await waitFor(() => expect(api.getStatus).toHaveBeenCalled());

    act(() => {
      window.dispatchEvent(new Event("pagehide"));
      window.dispatchEvent(new Event("pagehide"));
    });

    const exits = enqueued(delivery, "EXIT");
    expect(exits).toHaveLength(1);
    expect(exits[0][1]).toMatchObject({ reason: "page_closed" });
    expect(delivery.flushOnTeardown).toHaveBeenCalled();
  });

  it("sends a user EXIT from the error screen once, and none again at teardown", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    const api = buildApi({
      connect: async () => {
        throw new ApiError("Too many attempts", 429, "rate_limited");
      },
    });
    const delivery = buildDelivery();
    renderApp(api, delivery);

    await signIn("Anchor Point Bank", "alice", "hunter22");
    fireEvent.click(await screen.findByText("Exit"));
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });

    const exits = enqueued(delivery, "EXIT");
    expect(exits).toHaveLength(1);
    expect(exits[0][1]).toMatchObject({ reason: "user_exit", error_code: "rate_limited" });
  });

  it("does not report an exit after a completed connection", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    const api = buildApi({
      connect: async () => ({ status: "connected", public_token: "public-1" }),
    });
    const delivery = buildDelivery();
    renderApp(api, delivery);

    await signIn("Anchor Point Bank", "alice", "hunter22");
    await waitFor(() => expect(document.getElementById("step-success")).toHaveClass("active"));
    act(() => {
      window.dispatchEvent(new Event("pagehide"));
    });

    expect(enqueued(delivery, "CONNECTED")[0][1]).toMatchObject({ public_token: "public-1" });
    expect(enqueued(delivery, "EXIT")).toHaveLength(0);
    // The token is for the app, not the person using Link.
    expect(screen.queryByText("public-1")).toBeNull();
  });

  it("announces OPEN once and files telemetry under TELEMETRY with a name", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    const api = buildApi();
    const delivery = buildDelivery();
    renderApp(api, delivery);

    await signIn("Anchor Point Bank", "alice", "hunter22");
    await waitFor(() => expect(document.getElementById("step-success")).toHaveClass("active"));

    expect(enqueued(delivery, "OPEN")).toHaveLength(1);
    const telemetry = enqueued(delivery, "TELEMETRY").map(([, payload]) => payload);
    expect(telemetry.length).toBeGreaterThan(0);
    for (const payload of telemetry) {
      expect(payload).toHaveProperty("name");
      expect(payload).not.toHaveProperty("event");
    }
  });
});

describe("App provider forms", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("validates a security answer with the provider's rules, not the OTP pattern", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    const api = buildApi({
      connect: async () => ({
        status: "mfa_required",
        session_id: "mfa-1",
        mfa_type: "security_question",
        metadata: { message: "What was your first pet's name?" },
      }),
      submitMfa: async () => ({ status: "connected", public_token: "public-2" }),
    });
    const delivery = buildDelivery();
    renderApp(api, delivery);

    await signIn("Anchor Point Bank", "alice", "hunter22");
    await waitFor(() => expect(document.getElementById("step-mfa")).toHaveClass("active"));
    expect(screen.getByText("Answer your security question")).toBeInTheDocument();

    fireEvent.change(input("mfa-code"), { target: { value: "Rex the dog" } });
    fireEvent.click(document.getElementById("mfa-submit-btn")!);

    await waitFor(() => expect(document.getElementById("step-success")).toHaveClass("active"));
    expect(api.submitMfa).toHaveBeenCalledWith({ sessionId: "mfa-1", code: "Rex the dog" });
  });

  it("waits on the session for push approval instead of posting an empty code", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    let approved = false;
    const api = buildApi({
      connect: async () => ({ status: "mfa_required", session_id: "mfa-push", mfa_type: "push" }),
      getStatus: async () =>
        approved
          ? { status: "completed", public_token: "public-3" }
          : { status: "awaiting_institution" },
    });
    const delivery = buildDelivery();
    renderApp(api, delivery);

    await signIn("Anchor Point Bank", "alice", "hunter22");
    await waitFor(() => expect(document.getElementById("step-mfa")).toHaveClass("active"));
    expect(document.getElementById("mfa-code")).toBeNull();

    approved = true;
    fireEvent.click(screen.getByText("I approved it"));

    await waitFor(() => expect(document.getElementById("step-success")).toHaveClass("active"));
    expect(api.submitMfa).not.toHaveBeenCalled();
  });

  it("submits the credential form from the keyboard", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    const api = buildApi();
    const delivery = buildDelivery();
    renderApp(api, delivery);

    fireEvent.click(await screen.findByText("Anchor Point Bank"));
    fireEvent.change(input("link-username"), { target: { value: "alice" } });
    fireEvent.change(input("link-password"), { target: { value: "hunter22" } });
    fireEvent.submit(document.getElementById("credentials-form")!);

    await waitFor(() => expect(api.connect).toHaveBeenCalledTimes(1));
    expect(api.connect.mock.calls[0]).toEqual([
      { site: "hydro_one", encrypted: { username: "enc(alice)", password: "enc(hunter22)" } },
    ]);
  });
});

describe("App credential lifecycle", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("never carries one provider's password to another", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    const api = buildApi({
      connect: async () => {
        throw new ApiError("Invalid credentials", 401, "invalid_credentials");
      },
    });
    const delivery = buildDelivery();
    renderApp(api, delivery);

    await signIn("Anchor Point Bank", "alice", "bank-a-password");
    fireEvent.click(await screen.findByText("Choose a different provider"));
    fireEvent.click(await screen.findByText("GreenGrid Energy"));

    await waitFor(() => expect(document.getElementById("step-credentials")).toHaveClass("active"));
    expect(input("link-username").value).toBe("");
    expect(input("link-password").value).toBe("");
  });

  it("keeps the username but asks for the password again on retry", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    const api = buildApi({
      connect: async () => {
        throw new ApiError("Invalid credentials", 401, "invalid_credentials");
      },
    });
    const delivery = buildDelivery();
    renderApp(api, delivery);

    await signIn("Anchor Point Bank", "alice", "wrong-password");
    fireEvent.click(await screen.findByText("Try again"));

    await waitFor(() => expect(document.getElementById("step-credentials")).toHaveClass("active"));
    expect(screen.getByRole("heading", { name: "Anchor Point Bank" })).toBeInTheDocument();
    expect(input("link-username").value).toBe("alice");
    expect(input("link-password").value).toBe("");
  });
});

describe("App session setup", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("opens the session's provider directly when the session names one", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    const api = buildApi({
      getStatus: async () => ({ status: "awaiting_institution", site: "demo_utility" }),
      searchOrganizations: async (params) => ({
        results: params.site === "demo_utility" ? [utility] : [bank, utility],
      }),
    });
    const delivery = buildDelivery();
    renderApp(api, delivery);

    await waitFor(() => expect(document.getElementById("step-credentials")).toHaveClass("active"));
    expect(screen.getByRole("heading", { name: "GreenGrid Energy" })).toBeInTheDocument();
    expect(api.searchOrganizations).toHaveBeenCalledWith({ site: "demo_utility", limit: 1 });
    expect(enqueued(delivery, "INSTITUTION_SELECTED")[0][1]).toMatchObject({ site: "demo_utility" });
  });

  it("leaves the picker alone once a connection has started", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    const api = buildApi({
      getStatus: async () => ({ status: "connecting", site: "demo_utility" }),
    });
    const delivery = buildDelivery();
    renderApp(api, delivery);

    await waitFor(() => expect(api.getStatus).toHaveBeenCalled());
    expect(document.getElementById("step-select")).toHaveClass("active");
    expect(enqueued(delivery, "INSTITUTION_SELECTED")).toHaveLength(0);
  });

  it("refuses a link with two tokens and never opens a session", async () => {
    window.history.replaceState(null, "", "/link?token=victim&token=attacker");
    const apiFactory = vi.fn();
    const buildEventDelivery = vi.fn();
    render(
      <App
        seedInstitutions={[bank]}
        apiFactory={apiFactory}
        buildEventDelivery={buildEventDelivery}
      />,
    );

    await waitFor(() => expect(document.getElementById("step-error")).toHaveClass("active"));
    expect(document.getElementById("error-message")?.textContent).toContain("invalid");
    expect(apiFactory).not.toHaveBeenCalled();
    expect(buildEventDelivery).not.toHaveBeenCalled();
  });

  it("scopes parent-frame events to the session's allowed origins", async () => {
    window.history.replaceState(
      null,
      "",
      "/link?token=tok&origin=" + encodeURIComponent("https://attacker.example"),
    );
    const api = buildApi({
      getStatus: async () => ({
        status: "awaiting_institution",
        allowed_origins: ["https://merchant.example"],
      }),
    });
    const parent = { post: vi.fn(), resolve: vi.fn() };
    renderApp(api, buildDelivery(), { buildParentChannel: () => parent });

    await waitFor(() => expect(parent.resolve).toHaveBeenCalledWith(["https://merchant.example"]));
    expect(parent.post).toHaveBeenCalledWith(
      expect.objectContaining({ source: "plaidify-link", event: "OPEN" }),
    );
  });

  it("restricts parent-frame events to its own origin when the status fails", async () => {
    window.history.replaceState(null, "", "/link?token=tok");
    const api = buildApi({
      getStatus: async () => {
        throw new ApiError("Link session not found.", 404);
      },
    });
    const parent = { post: vi.fn(), resolve: vi.fn() };
    renderApp(api, buildDelivery(), { buildParentChannel: () => parent });

    await waitFor(() => expect(document.getElementById("step-error")).toHaveClass("active"));
    expect(parent.resolve).toHaveBeenCalledWith([]);
  });
});
