/**
 * Tests for the Plaidify JS SDK client.
 *
 * Every request is checked against the route it must hit on the server
 * (src/routers/*.py): method, path, query, auth header and body. Response
 * fixtures are shaped like the server's real replies.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { Plaidify } from "../src/client";
import { trimTrailingSlashes } from "../src/link-url";
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

function mockFetchError(body: unknown, status: number) {
  return vi.fn().mockResolvedValue({
    ok: false,
    status,
    json: () => Promise.resolve(body),
  });
}

interface SentRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | undefined;
}

function sent(index = 0): SentRequest {
  const call = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[index];
  return {
    url: call[0] as string,
    method: call[1].method,
    headers: call[1].headers,
    body: call[1].body,
  };
}

function sentJson(index = 0): unknown {
  const { body, headers } = sent(index);
  expect(headers["Content-Type"]).toBe("application/json");
  return JSON.parse(body as string);
}

const BASE = "http://localhost:8000";
let client: Plaidify;

beforeEach(() => {
  client = new Plaidify({ serverUrl: BASE, token: "jwt-user" });
  vi.restoreAllMocks();
});

// ── Health ───────────────────────────────────────────────────────────────────

describe("health", () => {
  it("returns health status", async () => {
    globalThis.fetch = mockFetch({ status: "healthy" });
    const result = await client.health();
    expect(result.status).toBe("healthy");
    expect(sent()).toMatchObject({ url: `${BASE}/health`, method: "GET", body: undefined });
  });
});

// ── Blueprints ───────────────────────────────────────────────────────────────

describe("blueprints", () => {
  it("lists blueprints", async () => {
    const data = {
      blueprints: [
        {
          site: "hydro_one",
          name: "GreenGrid Energy",
          domain: "greengrid.example.com",
          tags: ["utility"],
          has_mfa: true,
          schema_version: "2",
        },
      ],
      count: 1,
    };
    globalThis.fetch = mockFetch(data);
    const result = await client.listBlueprints();
    expect(result.count).toBe(1);
    expect(result.blueprints[0].site).toBe("hydro_one");
    expect(sent().url).toBe(`${BASE}/blueprints`);
  });

  it("gets a specific blueprint", async () => {
    const data = {
      name: "GreenGrid Energy",
      domain: "greengrid.example.com",
      tags: [],
      has_mfa: false,
      extract_fields: ["current_bill"],
      schema_version: "2",
      rate_limit: null,
    };
    globalThis.fetch = mockFetch(data);
    const result = await client.getBlueprint("hydro one");
    expect(result.extract_fields).toEqual(["current_bill"]);
    expect(sent().url).toBe(`${BASE}/blueprints/hydro%20one`);
  });
});

// ── Connect ──────────────────────────────────────────────────────────────────

describe("connect", () => {
  it("posts the credentials in a JSON body", async () => {
    const data = { status: "connected", job_id: "ajob-1", data: { current_bill: "$142.57" } };
    globalThis.fetch = mockFetch(data);
    const result = await client.connect("hydro_one", "user", "pass", {
      extractFields: ["current_bill"],
    });
    expect(result.status).toBe("connected");
    expect(result.data?.current_bill).toBe("$142.57");
    expect(sent()).toMatchObject({ url: `${BASE}/connect`, method: "POST" });
    expect(sentJson()).toEqual({
      site: "hydro_one",
      username: "user",
      password: "pass",
      extract_fields: ["current_bill"],
    });
  });

  it("returns MFA required status", async () => {
    const data = { status: "mfa_required", job_id: "ajob-2", session_id: "sess-123", mfa_type: "totp" };
    globalThis.fetch = mockFetch(data);
    const result = await client.connect("hydro_one", "fixture_mfa", "pass");
    expect(result.status).toBe("mfa_required");
    expect(result.session_id).toBe("sess-123");
  });
});

// ── MFA ──────────────────────────────────────────────────────────────────────

describe("submitMfa", () => {
  it("sends the code in a JSON body, never in the URL", async () => {
    globalThis.fetch = mockFetch({ status: "mfa_submitted", message: "Code submitted." });
    const result = await client.submitMfa("sess-123", "123456");
    expect(result.status).toBe("mfa_submitted");
    expect(sent()).toMatchObject({ url: `${BASE}/mfa/submit`, method: "POST" });
    expect(sentJson()).toEqual({ session_id: "sess-123", code: "123456" });
  });

  it("surfaces the server's error reply for an unknown session", async () => {
    globalThis.fetch = mockFetch({ status: "error", error: "MFA session not found or expired." });
    const result = await client.submitMfa("sess-x", "123456");
    expect(result).toEqual({ status: "error", error: "MFA session not found or expired." });
  });
});

// ── Access Jobs ─────────────────────────────────────────────────────────────

describe("access jobs", () => {
  it("lists access jobs with filters as query parameters", async () => {
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

    const result = await client.listAccessJobs({ limit: 5, site: "hydro_one", jobType: "connect" });
    expect(result.count).toBe(1);
    expect(sent().url).toBe(`${BASE}/access_jobs?limit=5&site=hydro_one&job_type=connect`);
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
    expect(sent().url).toBe(`${BASE}/access_jobs/ajob-2`);
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
  });
});

// ── Auth ─────────────────────────────────────────────────────────────────────

describe("register", () => {
  it("sends username, email and password, then uses the new token", async () => {
    const anonymous = new Plaidify({ serverUrl: BASE });
    globalThis.fetch = mockFetch({ access_token: "jwt-token", refresh_token: "ref", token_type: "bearer" });
    const result = await anonymous.register("alice", "alice@test.com", "Secure@pass123");
    expect(result.access_token).toBe("jwt-token");
    expect(sent()).toMatchObject({ url: `${BASE}/auth/register`, method: "POST" });
    expect(sent().headers.Authorization).toBeUndefined();
    expect(sentJson()).toEqual({
      username: "alice",
      email: "alice@test.com",
      password: "Secure@pass123",
    });

    globalThis.fetch = mockFetch({ id: 1, username: "alice", email: "alice@test.com", is_active: true });
    await anonymous.me();
    expect(sent().headers.Authorization).toBe("Bearer jwt-token");
  });

  it("returns the pending sign-up and keeps no token while the address is to be verified", async () => {
    const anonymous = new Plaidify({ serverUrl: BASE });
    const pending = {
      status: "verification_sent",
      detail: "If the address can be used, we sent it a link to finish signing up.",
    };
    globalThis.fetch = mockFetch(pending, 202);
    const result = await anonymous.register("alice", "alice@test.com", "Secure@pass123");
    expect(result).toEqual(pending);
    expect("access_token" in result).toBe(false);

    globalThis.fetch = mockFetch({ status: "healthy" });
    await anonymous.health();
    expect(sent().headers.Authorization).toBeUndefined();
  });
});

describe("verifyEmail", () => {
  it("posts the emailed token and the registration password and keeps the new token", async () => {
    const anonymous = new Plaidify({ serverUrl: BASE });
    globalThis.fetch = mockFetch({ access_token: "jwt-verified", refresh_token: "ref", token_type: "bearer" });
    const result = await anonymous.verifyEmail("mailed-token", "Secure@pass123");
    expect(result.access_token).toBe("jwt-verified");
    expect(sent()).toMatchObject({ url: `${BASE}/auth/verify-email`, method: "POST" });
    expect(sent().headers.Authorization).toBeUndefined();
    expect(sentJson()).toEqual({ token: "mailed-token", password: "Secure@pass123" });

    globalThis.fetch = mockFetch({ status: "healthy" });
    await anonymous.health();
    expect(sent().headers.Authorization).toBe("Bearer jwt-verified");
  });

  it("raises the server's error for a spent token", async () => {
    globalThis.fetch = mockFetchError({ detail: "Invalid or expired verification token" }, 400);
    await expect(new Plaidify({ serverUrl: BASE }).verifyEmail("spent", "Secure@pass123")).rejects.toMatchObject({
      message: "Invalid or expired verification token",
      statusCode: 400,
    });
  });
});

describe("login", () => {
  it("posts the OAuth2 password form to /auth/token and keeps the token", async () => {
    const anonymous = new Plaidify({ serverUrl: BASE });
    globalThis.fetch = mockFetch({ access_token: "jwt-token", refresh_token: "ref", token_type: "bearer" });
    const result = await anonymous.login("alice", "p@ss word&=");
    expect(result.access_token).toBe("jwt-token");

    const request = sent();
    expect(request).toMatchObject({ url: `${BASE}/auth/token`, method: "POST" });
    expect(request.headers["Content-Type"]).toBe("application/x-www-form-urlencoded");
    expect(Object.fromEntries(new URLSearchParams(request.body))).toEqual({
      username: "alice",
      password: "p@ss word&=",
    });

    globalThis.fetch = mockFetch({ status: "healthy" });
    await anonymous.health();
    expect(sent().headers.Authorization).toBe("Bearer jwt-token");
  });
});

describe("me", () => {
  it("returns user profile", async () => {
    globalThis.fetch = mockFetch({ id: 1, username: "alice", email: "user@test.com", is_active: true });
    const result = await client.me();
    expect(result).toEqual({ id: 1, username: "alice", email: "user@test.com", is_active: true });
    expect(sent().url).toBe(`${BASE}/auth/me`);
  });
});

// ── Link Flow ────────────────────────────────────────────────────────────────

describe("link flow", () => {
  it("creates a link session for a site", async () => {
    const data = { link_token: "lnk-abc", link_url: "/link?token=lnk-abc", public_key: "pem", expires_in: 1800, scopes: null };
    globalThis.fetch = mockFetch(data);
    const result = await client.createLinkSession("hydro_one");
    expect(result.link_token).toBe("lnk-abc");
    expect(sent()).toMatchObject({ url: `${BASE}/link/sessions?site=hydro_one`, method: "POST", body: undefined });
  });

  it("creates a link session without a site", async () => {
    globalThis.fetch = mockFetch({ link_token: "lnk-abc", link_url: "/link?token=lnk-abc", expires_in: 1800 });
    await client.createLinkSession();
    expect(sent().url).toBe(`${BASE}/link/sessions`);
  });

  it("creates a public link session", async () => {
    const data = { link_token: "lnk-public", link_url: "/link?token=lnk-public", expires_in: 1800 };
    globalThis.fetch = mockFetch(data);
    const result = await client.createPublicLinkSession();
    expect(result.link_token).toBe("lnk-public");
    expect(sent()).toMatchObject({ url: `${BASE}/link/sessions/public`, method: "POST" });
  });

  it("creates a hosted link bootstrap token", async () => {
    const data = {
      launch_token: "launch-123",
      expires_in: 300,
      site: "hydro_one",
      allowed_origin: "https://app.example.com",
      allowed_origins: ["https://app.example.com", "https://admin.example.com"],
      scopes: ["read_bill"],
    };
    globalThis.fetch = mockFetch(data);

    const result = await client.createHostedLinkBootstrap({
      site: "hydro_one",
      allowedOrigin: "https://app.example.com",
      allowedOrigins: ["https://app.example.com", "https://admin.example.com"],
      scopes: ["read_bill"],
    });

    expect(result.launch_token).toBe("launch-123");
    expect(sent()).toMatchObject({ url: `${BASE}/link/bootstrap`, method: "POST" });
    expect(sentJson()).toEqual({
      site: "hydro_one",
      allowed_origin: "https://app.example.com",
      allowed_origins: ["https://app.example.com", "https://admin.example.com"],
      scopes: ["read_bill"],
    });
  });

  it("exchanges a hosted link bootstrap token", async () => {
    const data = { link_token: "lnk-boot", link_url: "/link?token=lnk-boot", expires_in: 600 };
    globalThis.fetch = mockFetch(data);

    const result = await client.exchangeHostedLinkBootstrap("launch-123");
    expect(result.link_token).toBe("lnk-boot");
    expect(sent().url).toBe(`${BASE}/link/sessions/bootstrap`);
    expect(sentJson()).toEqual({ launch_token: "launch-123" });
  });

  it("generates link URL", () => {
    const url = client.getLinkUrl("lnk-abc");
    expect(url).toBe(`${BASE}/link?token=lnk-abc`);
  });

  it("generates themed link URL with every parameter the page reads", () => {
    const logo = "data:image/png;base64,iVBORw0KGgo=";
    const url = new URL(
      client.getLinkUrl("lnk-abc", {
        origin: "https://app.example.com",
        theme: { accentColor: "#0b8f73", bgColor: "#eef5ff", borderRadius: "30px", logo },
      }),
    );

    expect(Object.fromEntries(url.searchParams)).toEqual({
      token: "lnk-abc",
      origin: "https://app.example.com",
      accent: "#0b8f73",
      bg: "#eef5ff",
      radius: "30px",
      logo,
    });
  });

  it("exchanges public token", async () => {
    globalThis.fetch = mockFetch({ access_token: "acc-token-123" });
    const result = await client.exchangePublicToken("pub-token");
    expect(result.access_token).toBe("acc-token-123");
    expect(sent()).toMatchObject({ url: `${BASE}/exchange/public_token`, method: "POST" });
    expect(sentJson()).toEqual({ public_token: "pub-token" });
  });
});

// ── Links & tokens ───────────────────────────────────────────────────────────

describe("links and tokens", () => {
  it("lists links as the bare list the server returns", async () => {
    globalThis.fetch = mockFetch([{ link_token: "lnk-1", site: "hydro_one" }]);
    const links = await client.listLinks();
    expect(links).toEqual([{ link_token: "lnk-1", site: "hydro_one" }]);
  });

  it("lists access tokens as the bare list the server returns", async () => {
    globalThis.fetch = mockFetch([{ token: "acc-1", link_token: "lnk-1" }]);
    const tokens = await client.listTokens();
    expect(tokens[0].token).toBe("acc-1");
  });

  it("deletes a link", async () => {
    globalThis.fetch = mockFetch({ status: "Link and associated tokens deleted." });
    const result = await client.deleteLink("lnk-1");
    expect(result.status).toContain("deleted");
    expect(sent()).toMatchObject({ url: `${BASE}/links/lnk-1`, method: "DELETE" });
  });
});

// ── Webhooks ─────────────────────────────────────────────────────────────────

describe("webhooks", () => {
  it("registers a webhook with its signing secret at /webhooks/register", async () => {
    globalThis.fetch = mockFetch({ webhook_id: "wh-1", status: "registered" });
    const result = await client.registerWebhook("lnk-abc", "https://example.com/hook", "whsec-1");
    expect(result).toEqual({ webhook_id: "wh-1", status: "registered" });
    expect(sent()).toMatchObject({ url: `${BASE}/webhooks/register`, method: "POST" });
    expect(sentJson()).toEqual({
      link_token: "lnk-abc",
      url: "https://example.com/hook",
      secret: "whsec-1",
    });
  });

  it("lists webhooks", async () => {
    globalThis.fetch = mockFetch({ webhooks: [], count: 0 });
    const result = await client.listWebhooks();
    expect(result).toEqual({ webhooks: [], count: 0 });
  });

  it("tests a webhook", async () => {
    globalThis.fetch = mockFetch({ status: "delivered" });
    await client.testWebhook("wh-1");
    expect(sent().url).toBe(`${BASE}/webhooks/test`);
    expect(sentJson()).toEqual({ webhook_id: "wh-1" });
  });
});

// ── Agents ───────────────────────────────────────────────────────────────────

describe("agents", () => {
  it("registers an agent", async () => {
    const data = {
      agent_id: "agent-123",
      name: "Test Agent",
      api_key: "pk_agent_abc",
      api_key_prefix: "pk_agent_abc",
      allowed_scopes: ["read:bill"],
      allowed_sites: null,
    };
    globalThis.fetch = mockFetch(data);
    const result = await client.registerAgent("Test Agent", {
      description: "A test agent",
      allowedScopes: ["read:bill"],
    });
    expect(result.api_key).toBe("pk_agent_abc");
    expect(sentJson()).toEqual({
      name: "Test Agent",
      description: "A test agent",
      allowed_scopes: ["read:bill"],
    });
  });

  it("lists agents", async () => {
    const data = { agents: [{ agent_id: "agent-1", name: "Agent1" }], count: 1 };
    globalThis.fetch = mockFetch(data);
    const result = await client.listAgents();
    expect(result.count).toBe(1);
  });

  it("updates only the fields given", async () => {
    globalThis.fetch = mockFetch({ status: "updated", agent_id: "agent-1" });
    await client.updateAgent("agent-1", { allowedSites: ["hydro_one"] });
    expect(sent()).toMatchObject({ url: `${BASE}/agents/agent-1`, method: "PATCH" });
    expect(sentJson()).toEqual({ allowed_sites: ["hydro_one"] });
  });

  it("deactivates an agent", async () => {
    globalThis.fetch = mockFetch({ status: "deactivated", agent_id: "agent-1" });
    const result = await client.deactivateAgent("agent-1");
    expect(result.status).toBe("deactivated");
    expect(sent().method).toBe("DELETE");
  });
});

// ── Consent ──────────────────────────────────────────────────────────────────

describe("consent", () => {
  it("requests consent and returns the server's request_id", async () => {
    const data = {
      request_id: "creq-42",
      agent_name: "Agent",
      scopes: ["read:bill"],
      duration_seconds: 3600,
      status: "pending",
    };
    globalThis.fetch = mockFetch(data);
    const result = await client.requestConsent("acc-token", ["read:bill"], "Agent", 3600);
    expect(result.request_id).toBe("creq-42");
    expect(sentJson()).toEqual({
      access_token: "acc-token",
      scopes: ["read:bill"],
      agent_name: "Agent",
      duration_seconds: 3600,
    });
  });

  it("approves consent by request id", async () => {
    const data = {
      consent_token: "consent-xyz",
      scopes: ["read:bill"],
      expires_at: "2026-10-01T00:00:00+00:00",
      status: "approved",
    };
    globalThis.fetch = mockFetch(data);
    const result = await client.approveConsent("creq-42");
    expect(result.consent_token).toBe("consent-xyz");
    expect(sent()).toMatchObject({ url: `${BASE}/consent/creq-42/approve`, method: "POST" });
  });

  it("lists consent grants", async () => {
    globalThis.fetch = mockFetch({ grants: [], count: 0 });
    const result = await client.listConsents();
    expect(result).toEqual({ grants: [], count: 0 });
  });
});

// ── API Keys ─────────────────────────────────────────────────────────────────

describe("api keys", () => {
  it("creates an API key with scopes as a list and expires_days", async () => {
    const data = {
      id: "key-1",
      name: "test-key",
      key: "pk_abc123",
      key_prefix: "pk_abc123",
      expires_at: "2026-10-29T00:00:00+00:00",
      created_at: "2026-09-29T00:00:00+00:00",
    };
    globalThis.fetch = mockFetch(data);
    const result = await client.createApiKey("test-key", { scopes: ["read:bill"], expiresDays: 30 });
    expect(result.key).toBe("pk_abc123");
    expect(sent()).toMatchObject({ url: `${BASE}/api-keys`, method: "POST" });
    expect(sentJson()).toEqual({ name: "test-key", scopes: ["read:bill"], expires_days: 30 });
  });

  it("lists API keys from the bare list, with scopes as a list", async () => {
    globalThis.fetch = mockFetch([
      {
        id: "key-1",
        name: "scoped",
        key_prefix: "pk_1",
        scopes: '["read:bill"]',
        expires_at: null,
        last_used_at: null,
        created_at: null,
      },
      {
        id: "key-2",
        name: "open",
        key_prefix: "pk_2",
        scopes: null,
        expires_at: null,
        last_used_at: null,
        created_at: null,
      },
    ]);
    const keys = await client.listApiKeys();
    expect(keys.map((key) => key.scopes)).toEqual([["read:bill"], null]);
  });

  it("revokes an API key", async () => {
    globalThis.fetch = mockFetch({ status: "revoked", key_id: "key-1" });
    const result = await client.revokeApiKey("key-1");
    expect(result).toEqual({ status: "revoked", key_id: "key-1" });
    expect(sent().method).toBe("DELETE");
  });
});

// ── Audit ────────────────────────────────────────────────────────────────────

describe("audit", () => {
  it("gets audit logs", async () => {
    const data = { entries: [], total: 0, offset: 0, limit: 100 };
    globalThis.fetch = mockFetch(data);
    const result = await client.getAuditLogs({ eventType: "auth" });
    expect(result.total).toBe(0);
    expect(sent().url).toBe(`${BASE}/audit/logs?event_type=auth&offset=0&limit=100`);
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
    const data = {
      status: "scheduled",
      access_token: "acc-123...",
      interval_seconds: 3600,
      schedule_format: "interval",
    };
    globalThis.fetch = mockFetch(data);
    const result = await client.scheduleRefresh("acc-123");
    expect(result.interval_seconds).toBe(3600);
    expect(sentJson()).toEqual({ access_token: "acc-123", interval_seconds: 3600 });
  });

  it("unschedules a refresh", async () => {
    globalThis.fetch = mockFetch({ status: "unscheduled", access_token: "acc-123..." });
    const result = await client.unscheduleRefresh("acc-123");
    expect(result.status).toBe("unscheduled");
  });

  it("lists the caller's schedules as an array", async () => {
    const job = {
      access_token: "acc-123...",
      interval_seconds: 3600,
      schedule_format: "interval",
      enabled: false,
      disabled_reason: "needs_reauth",
      last_refreshed: null,
      next_run_at: null,
      last_error: "mfa_required",
      consecutive_failures: 1,
    };
    globalThis.fetch = mockFetch({ jobs: [job] });
    const result = await client.listRefreshJobs();
    expect(result.jobs).toEqual([job]);
    expect(result.jobs[0].disabled_reason).toBe("needs_reauth");
    expect(sent().url).toBe(`${BASE}/refresh/jobs`);
  });
});

// ── Fetch Data ───────────────────────────────────────────────────────────────

describe("fetchData", () => {
  it("POSTs the tokens in a JSON body, never the query string", async () => {
    globalThis.fetch = mockFetch({ status: "connected", data: { current_bill: "$12" }, job_id: "ajob-9" });
    const result = await client.fetchData("acc-123", "consent-9");
    expect(result.data?.current_bill).toBe("$12");
    const request = sent();
    expect(request).toMatchObject({ url: `${BASE}/fetch_data`, method: "POST" });
    expect(request.url).not.toContain("acc-123");
    expect(sentJson()).toEqual({ access_token: "acc-123", consent_token: "consent-9" });
  });

  it("omits the consent token when there is none", async () => {
    globalThis.fetch = mockFetch({ status: "connected", data: {} });
    await client.fetchData("acc-123");
    expect(sentJson()).toEqual({ access_token: "acc-123" });
  });
});

// ── Error Handling ───────────────────────────────────────────────────────────

describe("error handling", () => {
  it("throws AuthenticationError on 401", async () => {
    globalThis.fetch = mockFetchError({ detail: "Unauthorized" }, 401);
    await expect(client.health()).rejects.toThrow(AuthenticationError);
  });

  it("throws NotFoundError on 404", async () => {
    globalThis.fetch = mockFetchError({ detail: "Not found" }, 404);
    await expect(client.getBlueprint("nonexistent")).rejects.toThrow(NotFoundError);
  });

  it("throws RateLimitError on 429", async () => {
    globalThis.fetch = mockFetchError({ detail: "Too many requests" }, 429);
    await expect(client.health()).rejects.toThrow(RateLimitError);
  });

  it("throws ServerError on 500", async () => {
    globalThis.fetch = mockFetchError({ detail: "Internal error" }, 500);
    await expect(client.health()).rejects.toThrow(ServerError);
  });

  it("throws PlaidifyError on other status codes", async () => {
    globalThis.fetch = mockFetchError({ detail: "Bad request" }, 400);
    await expect(client.health()).rejects.toThrow(PlaidifyError);
  });

  it("reads FastAPI validation errors instead of printing [object Object]", async () => {
    globalThis.fetch = mockFetchError(
      {
        detail: [
          { loc: ["body", "username"], msg: "Field required", type: "missing" },
          { loc: ["body", "password"], msg: "String should have at least 8 characters", type: "string_too_short" },
        ],
      },
      422,
    );
    await expect(client.register("a", "a@b.c", "x")).rejects.toThrow(
      "Field required; String should have at least 8 characters",
    );
  });

  it("reads Plaidify's {error, error_code} body", async () => {
    globalThis.fetch = mockFetchError({ error: "Site is down", error_code: "institution_down" }, 503);
    const error = await client.health().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ServerError);
    expect((error as ServerError).message).toBe("Site is down");
    expect((error as ServerError).errorCode).toBe("institution_down");
  });
});

// ── Headers / Auth ───────────────────────────────────────────────────────────

describe("authentication headers", () => {
  it("sends a user token as a bearer token", async () => {
    globalThis.fetch = mockFetch({ status: "healthy" });
    await client.health();
    expect(sent().headers.Authorization).toBe("Bearer jwt-user");
    expect(sent().headers["X-API-Key"]).toBeUndefined();
  });

  it("sends an API key in X-API-Key", async () => {
    const keyClient = new Plaidify({ serverUrl: BASE, apiKey: "pk_test_key" });
    globalThis.fetch = mockFetch({ status: "healthy" });
    await keyClient.health();
    expect(sent().headers["X-API-Key"]).toBe("pk_test_key");
    expect(sent().headers.Authorization).toBeUndefined();
  });

  it("sends a pk_ key passed as `token` in X-API-Key too", async () => {
    const keyClient = new Plaidify({ serverUrl: BASE, token: "pk_agent_abc" });
    globalThis.fetch = mockFetch({ status: "healthy" });
    await keyClient.health();
    expect(sent().headers["X-API-Key"]).toBe("pk_agent_abc");
    expect(sent().headers.Authorization).toBeUndefined();
  });

  it("prefers token over apiKey", async () => {
    const bothClient = new Plaidify({ serverUrl: BASE, token: "jwt", apiKey: "pk_key" });
    globalThis.fetch = mockFetch({ status: "healthy" });
    await bothClient.health();
    expect(sent().headers.Authorization).toBe("Bearer jwt");
    expect(sent().headers["X-API-Key"]).toBeUndefined();
  });

  it("setToken updates the bearer token", async () => {
    globalThis.fetch = mockFetch({ status: "healthy" });
    client.setToken("new-token");
    await client.health();
    expect(sent().headers.Authorization).toBe("Bearer new-token");
  });

  it("sends no Content-Type on requests without a body", async () => {
    globalThis.fetch = mockFetch({ status: "healthy" });
    await client.health();
    expect(sent().headers["Content-Type"]).toBeUndefined();
  });
});

// ── URL Construction ─────────────────────────────────────────────────────────

describe("URL construction", () => {
  it("strips trailing slashes from serverUrl", async () => {
    const slashClient = new Plaidify({ serverUrl: `${BASE}///` });
    globalThis.fetch = mockFetch({ status: "healthy" });
    await slashClient.health();
    expect(sent().url).toBe(`${BASE}/health`);
  });

  it("trims a long run of slashes in linear time", () => {
    const url = `${BASE}${"/".repeat(100_000)}`;
    const started = performance.now();
    expect(trimTrailingSlashes(url)).toBe(BASE);
    expect(trimTrailingSlashes(`${"/".repeat(100_000)}x`)).toBe(`${"/".repeat(100_000)}x`);
    expect(performance.now() - started).toBeLessThan(250);
  });
});

// ── Error classes ────────────────────────────────────────────────────────────

describe("error classes", () => {
  it("PlaidifyError has statusCode and detail", () => {
    const err = new PlaidifyError("test error", 418, "teapot");
    expect(err.message).toBe("test error");
    expect(err.statusCode).toBe(418);
    expect(err.detail).toBe("test error");
    expect(err.errorCode).toBe("teapot");
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
