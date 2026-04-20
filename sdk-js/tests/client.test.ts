/**
 * Tests for the Plaidify JS SDK client.
 *
 * Uses vitest with fetch mocking to test each client method.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { Plaidify } from "../src/client";
import {
  PlaidifyError,
  AuthenticationError,
  NotFoundError,
  RateLimitError,
  ServerError,
} from "../src/errors";

// ── Test helpers ─────────────────────────────────────────────────────────────

function mockFetch(data: unknown, status = 200) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(data),
  });
}

function mockFetchError(detail: string, status: number) {
  return vi.fn().mockResolvedValue({
    ok: false,
    status,
    json: () => Promise.resolve({ detail }),
  });
}

let client: Plaidify;

beforeEach(() => {
  client = new Plaidify({ serverUrl: "http://localhost:8000" });
  vi.restoreAllMocks();
});

// ── Health ───────────────────────────────────────────────────────────────────

describe("health", () => {
  it("returns health status", async () => {
    globalThis.fetch = mockFetch({ status: "healthy", version: "0.3.0" });
    const result = await client.health();
    expect(result.status).toBe("healthy");
    expect(result.version).toBe("0.3.0");
  });
});

// ── Blueprints ───────────────────────────────────────────────────────────────

describe("listBlueprints", () => {
  it("returns blueprint list", async () => {
    const data = {
      blueprints: [
        { name: "GreenGrid Energy", site: "hydro_one", domain: "greengrid.example.com" },
      ],
      count: 1,
    };
    globalThis.fetch = mockFetch(data);
    const result = await client.listBlueprints();
    expect(result.count).toBe(1);
    expect(result.blueprints[0].name).toBe("GreenGrid Energy");
  });
});

describe("getBlueprint", () => {
  it("returns a specific blueprint", async () => {
    const data = { name: "GreenGrid Energy", domain: "greengrid.example.com" };
    globalThis.fetch = mockFetch(data);
    const result = await client.getBlueprint("hydro_one");
    expect(result.name).toBe("GreenGrid Energy");
  });
});

// ── Connect ──────────────────────────────────────────────────────────────────

describe("connect", () => {
  it("connects and returns data", async () => {
    const data = { status: "connected", job_id: "ajob-1", data: { current_bill: "$142.57" } };
    globalThis.fetch = mockFetch(data);
    const result = await client.connect("hydro_one", "user", "pass");
    expect(result.status).toBe("connected");
    expect(result.job_id).toBe("ajob-1");
    expect(result.data?.current_bill).toBe("$142.57");
  });

  it("returns MFA required status", async () => {
    const data = { status: "mfa_required", job_id: "ajob-2", session_id: "sess-123", mfa_type: "totp" };
    globalThis.fetch = mockFetch(data);
    const result = await client.connect("hydro_one", "fixture_mfa", "pass");
    expect(result.status).toBe("mfa_required");
    expect(result.job_id).toBe("ajob-2");
    expect(result.session_id).toBe("sess-123");
  });

  it("returns pending status with job_id", async () => {
    const data = {
      status: "pending",
      job_id: "ajob-3",
      session_id: "access-session-1",
      metadata: { message: "Still running" },
    };
    globalThis.fetch = mockFetch(data);
    const result = await client.connect("hydro_one", "user", "pass");
    expect(result.status).toBe("pending");
    expect(result.job_id).toBe("ajob-3");
    expect(result.metadata?.message).toBe("Still running");
  });

  it("passes extract_fields option", async () => {
    globalThis.fetch = mockFetch({ status: "connected", data: {} });
    await client.connect("hydro_one", "user", "pass", { extractFields: ["current_bill"] });

    const call = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const body = JSON.parse(call[1].body);
    expect(body.extract_fields).toEqual(["current_bill"]);
  });
});

// ── MFA ──────────────────────────────────────────────────────────────────────

describe("submitMfa", () => {
  it("submits MFA code", async () => {
    globalThis.fetch = mockFetch({ status: "connected", data: {} });
    const result = await client.submitMfa("sess-123", "123456");
    expect(result.status).toBe("connected");
  });
});

// ── Access Jobs ─────────────────────────────────────────────────────────────

describe("access jobs", () => {
  it("lists access jobs", async () => {
    globalThis.fetch = mockFetch({
      jobs: [
        {
          job_id: "ajob-1",
          site: "hydro_one",
          job_type: "connect",
          status: "completed",
          result: { status: "connected", data: { current_bill: "$142.57" } },
        },
      ],
      count: 1,
    });

    const result = await client.listAccessJobs();
    expect(result.count).toBe(1);
    expect(result.jobs[0].job_id).toBe("ajob-1");
  });

  it("gets a specific access job", async () => {
    globalThis.fetch = mockFetch({
      job_id: "ajob-2",
      site: "hydro_one",
      job_type: "connect",
      status: "completed",
      result: { status: "connected", data: { current_bill: "$142.57" } },
    });

    const result = await client.getAccessJob("ajob-2");
    expect(result.job_id).toBe("ajob-2");
    expect(result.result?.status).toBe("connected");
  });

  it("waits for access job completion", async () => {
    globalThis.fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          job_id: "ajob-3",
          site: "hydro_one",
          job_type: "connect",
          status: "running",
        }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: () => Promise.resolve({
          job_id: "ajob-3",
          site: "hydro_one",
          job_type: "connect",
          status: "completed",
          result: { status: "connected", data: { current_bill: "$142.57" } },
        }),
      });

    const result = await client.waitForAccessJob("ajob-3", { pollIntervalMs: 0, timeoutMs: 100 });
    expect(result.status).toBe("completed");
    expect(result.result?.status).toBe("connected");
  });
});

// ── Auth ─────────────────────────────────────────────────────────────────────

describe("register", () => {
  it("registers and stores token", async () => {
    const data = { access_token: "jwt-token", refresh_token: "ref-token", token_type: "bearer" };
    globalThis.fetch = mockFetch(data);
    const result = await client.register("user@test.com", "password123");
    expect(result.access_token).toBe("jwt-token");
  });
});

describe("login", () => {
  it("logs in and stores token", async () => {
    const data = { access_token: "jwt-token", refresh_token: "ref-token", token_type: "bearer" };
    globalThis.fetch = mockFetch(data);
    const result = await client.login("user@test.com", "password123");
    expect(result.access_token).toBe("jwt-token");
  });
});

describe("me", () => {
  it("returns user profile", async () => {
    globalThis.fetch = mockFetch({ id: 1, email: "user@test.com" });
    const result = await client.me();
    expect(result.id).toBe(1);
  });
});

// ── Link Flow ────────────────────────────────────────────────────────────────

describe("link flow", () => {
  it("creates a link session", async () => {
    const data = { link_token: "lnk-abc", link_url: "/link?token=lnk-abc", expires_in: 600 };
    globalThis.fetch = mockFetch(data);
    const result = await client.createLinkSession("hydro_one");
    expect(result.link_token).toBe("lnk-abc");
  });

  it("creates a public link session", async () => {
    const data = { link_token: "lnk-public", link_url: "/link?token=lnk-public", expires_in: 600 };
    globalThis.fetch = mockFetch(data);
    const result = await client.createPublicLinkSession();
    expect(result.link_token).toBe("lnk-public");
  });

  it("creates a hosted link bootstrap token", async () => {
    const data = {
      launch_token: "launch-123",
      expires_in: 300,
      site: "hydro_one",
      allowed_origin: "https://app.example.com",
      scopes: ["read_bill"],
    };
    globalThis.fetch = mockFetch(data);

    const result = await client.createHostedLinkBootstrap({
      site: "hydro_one",
      allowedOrigin: "https://app.example.com",
      scopes: ["read_bill"],
    });

    expect(result.launch_token).toBe("launch-123");

    const call = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const body = JSON.parse(call[1].body);
    expect(body.allowed_origin).toBe("https://app.example.com");
    expect(body.site).toBe("hydro_one");
    expect(body.scopes).toEqual(["read_bill"]);
  });

  it("exchanges a hosted link bootstrap token", async () => {
    const data = { link_token: "lnk-boot", link_url: "/link?token=lnk-boot", expires_in: 600 };
    globalThis.fetch = mockFetch(data);

    const result = await client.exchangeHostedLinkBootstrap("launch-123");
    expect(result.link_token).toBe("lnk-boot");
  });

  it("generates link URL", () => {
    const url = client.getLinkUrl("lnk-abc");
    expect(url).toBe("http://localhost:8000/link?token=lnk-abc");
  });

  it("generates themed link URL", () => {
    const url = client.getLinkUrl("lnk-abc", {
      origin: "myapp://callback",
      theme: {
        accentColor: "#0b8f73",
        borderRadius: "30px",
      },
    });

    expect(url).toContain("token=lnk-abc");
    expect(url).toContain("origin=myapp%3A%2F%2Fcallback");
    expect(url).toContain("accent=%230b8f73");
    expect(url).toContain("radius=30px");
  });

  it("exchanges public token", async () => {
    globalThis.fetch = mockFetch({ access_token: "acc-token-123" });
    const result = await client.exchangePublicToken("pub-token");
    expect(result.access_token).toBe("acc-token-123");
  });
});

// ── Webhooks ─────────────────────────────────────────────────────────────────

describe("webhooks", () => {
  it("registers a webhook", async () => {
    const data = { webhook_id: "wh-1", url: "https://example.com/hook", status: "active" };
    globalThis.fetch = mockFetch(data);
    const result = await client.registerWebhook("lnk-abc", "https://example.com/hook");
    expect(result.webhook_id).toBe("wh-1");
  });

  it("lists webhooks", async () => {
    globalThis.fetch = mockFetch({ webhooks: [] });
    const result = await client.listWebhooks();
    expect(result.webhooks).toEqual([]);
  });
});

// ── Agents ───────────────────────────────────────────────────────────────────

describe("agents", () => {
  it("registers an agent", async () => {
    const data = { agent_id: "agent-123", name: "Test Agent" };
    globalThis.fetch = mockFetch(data);
    const result = await client.registerAgent("Test Agent", { description: "A test agent" });
    expect(result.agent_id).toBe("agent-123");
    expect(result.name).toBe("Test Agent");
  });

  it("lists agents", async () => {
    const data = { agents: [{ agent_id: "agent-1", name: "Agent1" }], count: 1 };
    globalThis.fetch = mockFetch(data);
    const result = await client.listAgents();
    expect(result.count).toBe(1);
  });

  it("gets a specific agent", async () => {
    globalThis.fetch = mockFetch({ agent_id: "agent-1", name: "Agent1" });
    const result = await client.getAgent("agent-1");
    expect(result.agent_id).toBe("agent-1");
  });

  it("deactivates an agent", async () => {
    globalThis.fetch = mockFetch({ status: "deactivated", agent_id: "agent-1" });
    const result = await client.deactivateAgent("agent-1");
    expect(result.status).toBe("deactivated");
  });
});

// ── Consent ──────────────────────────────────────────────────────────────────

describe("consent", () => {
  it("requests consent", async () => {
    const data = { consent_request_id: 42, status: "pending" };
    globalThis.fetch = mockFetch(data);
    const result = await client.requestConsent("acc-token", ["read:bill"], "Agent", 3600);
    expect(result.consent_request_id).toBe(42);
    expect(result.status).toBe("pending");
  });

  it("approves consent", async () => {
    const data = { consent_token: "ct-xyz", scopes: ["read:bill"] };
    globalThis.fetch = mockFetch(data);
    const result = await client.approveConsent(42);
    expect(result.consent_token).toBe("ct-xyz");
  });
});

// ── API Keys ─────────────────────────────────────────────────────────────────

describe("api keys", () => {
  it("creates an API key", async () => {
    const data = { id: "key-1", name: "test-key", key_prefix: "pk_test_", raw_key: "pk_test_abc123" };
    globalThis.fetch = mockFetch(data);
    const result = await client.createApiKey("test-key");
    expect(result.id).toBe("key-1");
    expect(result.raw_key).toBe("pk_test_abc123");
  });

  it("lists API keys", async () => {
    globalThis.fetch = mockFetch({ keys: [{ id: "key-1", name: "test-key", key_prefix: "pk_" }] });
    const result = await client.listApiKeys();
    expect(result.keys).toHaveLength(1);
  });
});

// ── Audit ────────────────────────────────────────────────────────────────────

describe("audit", () => {
  it("gets audit logs", async () => {
    const data = { entries: [], total: 0, offset: 0, limit: 100 };
    globalThis.fetch = mockFetch(data);
    const result = await client.getAuditLogs();
    expect(result.total).toBe(0);
  });

  it("verifies audit chain", async () => {
    globalThis.fetch = mockFetch({ valid: true, total: 50, errors: [] });
    const result = await client.verifyAuditChain();
    expect(result.valid).toBe(true);
  });
});

// ── Scheduled Refresh ────────────────────────────────────────────────────────

describe("scheduled refresh", () => {
  it("schedules a refresh", async () => {
    const data = { status: "scheduled", access_token: "acc-123", interval_seconds: 3600 };
    globalThis.fetch = mockFetch(data);
    const result = await client.scheduleRefresh("acc-123");
    expect(result.status).toBe("scheduled");
    expect(result.interval_seconds).toBe(3600);
  });

  it("unschedules a refresh", async () => {
    globalThis.fetch = mockFetch({ status: "unscheduled" });
    const result = await client.unscheduleRefresh("acc-123");
    expect(result.status).toBe("unscheduled");
  });
});

// ── Error Handling ───────────────────────────────────────────────────────────

describe("error handling", () => {
  it("throws AuthenticationError on 401", async () => {
    globalThis.fetch = mockFetchError("Unauthorized", 401);
    await expect(client.health()).rejects.toThrow(AuthenticationError);
  });

  it("throws NotFoundError on 404", async () => {
    globalThis.fetch = mockFetchError("Not found", 404);
    await expect(client.getBlueprint("nonexistent")).rejects.toThrow(NotFoundError);
  });

  it("throws RateLimitError on 429", async () => {
    globalThis.fetch = mockFetchError("Too many requests", 429);
    await expect(client.health()).rejects.toThrow(RateLimitError);
  });

  it("throws ServerError on 500", async () => {
    globalThis.fetch = mockFetchError("Internal error", 500);
    await expect(client.health()).rejects.toThrow(ServerError);
  });

  it("throws PlaidifyError on other status codes", async () => {
    globalThis.fetch = mockFetchError("Bad request", 400);
    await expect(client.health()).rejects.toThrow(PlaidifyError);
  });
});

// ── Headers / Auth ───────────────────────────────────────────────────────────

describe("authentication headers", () => {
  it("sends bearer token when set", async () => {
    const tokenClient = new Plaidify({ serverUrl: "http://localhost:8000", token: "my-jwt" });
    globalThis.fetch = mockFetch({ status: "healthy" });
    await tokenClient.health();

    const call = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[1].headers["Authorization"]).toBe("Bearer my-jwt");
  });

  it("sends API key when set", async () => {
    const keyClient = new Plaidify({ serverUrl: "http://localhost:8000", apiKey: "pk_test_key" });
    globalThis.fetch = mockFetch({ status: "healthy" });
    await keyClient.health();

    const call = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[1].headers["X-API-Key"]).toBe("pk_test_key");
  });

  it("prefers token over apiKey", async () => {
    const bothClient = new Plaidify({ serverUrl: "http://localhost:8000", token: "jwt", apiKey: "pk_key" });
    globalThis.fetch = mockFetch({ status: "healthy" });
    await bothClient.health();

    const call = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[1].headers["Authorization"]).toBe("Bearer jwt");
    expect(call[1].headers["X-API-Key"]).toBeUndefined();
  });

  it("setToken updates the bearer token", async () => {
    globalThis.fetch = mockFetch({ status: "healthy" });
    client.setToken("new-token");
    await client.health();

    const call = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[1].headers["Authorization"]).toBe("Bearer new-token");
  });
});

// ── URL Construction ─────────────────────────────────────────────────────────

describe("URL construction", () => {
  it("strips trailing slashes from serverUrl", async () => {
    const slashClient = new Plaidify({ serverUrl: "http://localhost:8000///" });
    globalThis.fetch = mockFetch({ status: "healthy" });
    await slashClient.health();

    const call = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(call[0]).toBe("http://localhost:8000/health");
  });
});

// ── Error classes ────────────────────────────────────────────────────────────

describe("error classes", () => {
  it("PlaidifyError has statusCode and detail", () => {
    const err = new PlaidifyError("test error", 418);
    expect(err.message).toBe("test error");
    expect(err.statusCode).toBe(418);
    expect(err.detail).toBe("test error");
    expect(err.name).toBe("PlaidifyError");
  });

  it("AuthenticationError defaults to 401", () => {
    const err = new AuthenticationError();
    expect(err.statusCode).toBe(401);
    expect(err.name).toBe("AuthenticationError");
  });

  it("errors are instanceof PlaidifyError", () => {
    expect(new NotFoundError()).toBeInstanceOf(PlaidifyError);
    expect(new RateLimitError()).toBeInstanceOf(PlaidifyError);
    expect(new ServerError()).toBeInstanceOf(PlaidifyError);
  });
});
