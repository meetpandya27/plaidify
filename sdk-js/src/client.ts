/**
 * Plaidify TypeScript/JavaScript API client.
 *
 * @example
 * ```ts
 * import { Plaidify } from "@plaidify/client";
 *
 * const pfy = new Plaidify({ serverUrl: "http://localhost:8000" });
 * await pfy.login("user@example.com", "password");
 * const blueprints = await pfy.listBlueprints();
 * ```
 */

import type {
  PlaidifyConfig,
  HealthStatus,
  BlueprintInfo,
  BlueprintListResult,
  ConnectResult,
  AuthToken,
  UserProfile,
  LinkSession,
  WebhookRegistration,
  AgentInfo,
  AgentListResult,
  ConsentRequest,
  ConsentGrant,
  ApiKeyInfo,
  AuditLogResult,
  AuditVerifyResult,
  WebhookDeliveryResult,
  PublicTokenExchangeResult,
  RefreshScheduleResult,
} from "./types";

import {
  PlaidifyError,
  AuthenticationError,
  NotFoundError,
  RateLimitError,
  ServerError,
} from "./errors";

// ── HTTP helpers ─────────────────────────────────────────────────────────────

async function raiseForStatus(response: Response): Promise<void> {
  if (response.ok) return;

  let detail = `HTTP ${response.status}`;
  try {
    const body = await response.json();
    detail = body.detail || detail;
  } catch {
    // no JSON body
  }

  switch (response.status) {
    case 401:
      throw new AuthenticationError(detail);
    case 404:
      throw new NotFoundError(detail);
    case 429:
      throw new RateLimitError(detail);
    default:
      if (response.status >= 500) throw new ServerError(detail);
      throw new PlaidifyError(detail, response.status);
  }
}

// ── Client ───────────────────────────────────────────────────────────────────

export class Plaidify {
  private readonly baseUrl: string;
  private readonly timeout: number;
  private token?: string;
  private apiKey?: string;

  constructor(config: PlaidifyConfig) {
    this.baseUrl = config.serverUrl.replace(/\/+$/, "");
    this.timeout = config.timeout ?? 30_000;
    this.token = config.token;
    this.apiKey = config.apiKey;
  }

  /** Update the bearer token (e.g. after login). */
  setToken(token: string): void {
    this.token = token;
  }

  // ── HTTP layer ─────────────────────────────────────────────────────────

  private headers(): Record<string, string> {
    const h: Record<string, string> = { "Content-Type": "application/json" };
    if (this.token) h["Authorization"] = `Bearer ${this.token}`;
    else if (this.apiKey) h["X-API-Key"] = this.apiKey;
    return h;
  }

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    params?: Record<string, string | number>,
  ): Promise<T> {
    let url = `${this.baseUrl}${path}`;
    if (params) {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null) qs.set(k, String(v));
      }
      const qsStr = qs.toString();
      if (qsStr) url += `?${qsStr}`;
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);

    try {
      const response = await fetch(url, {
        method,
        headers: this.headers(),
        body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
      await raiseForStatus(response);
      return (await response.json()) as T;
    } finally {
      clearTimeout(timer);
    }
  }

  private get<T>(path: string, params?: Record<string, string | number>): Promise<T> {
    return this.request<T>("GET", path, undefined, params);
  }

  private post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>("POST", path, body);
  }

  private patch<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>("PATCH", path, body);
  }

  private del<T>(path: string): Promise<T> {
    return this.request<T>("DELETE", path);
  }

  // ── Health ─────────────────────────────────────────────────────────────

  async health(): Promise<HealthStatus> {
    return this.get<HealthStatus>("/health");
  }

  // ── Blueprints ─────────────────────────────────────────────────────────

  async listBlueprints(): Promise<BlueprintListResult> {
    return this.get<BlueprintListResult>("/blueprints");
  }

  async getBlueprint(name: string): Promise<BlueprintInfo> {
    return this.get<BlueprintInfo>(`/blueprints/${encodeURIComponent(name)}`);
  }

  // ── Connect ────────────────────────────────────────────────────────────

  async connect(
    site: string,
    username: string,
    password: string,
    options?: { extractFields?: string[] },
  ): Promise<ConnectResult> {
    return this.post<ConnectResult>("/connect", {
      site,
      username,
      password,
      extract_fields: options?.extractFields,
    });
  }

  async submitMfa(sessionId: string, code: string): Promise<ConnectResult> {
    return this.post<ConnectResult>("/mfa/submit", {
      session_id: sessionId,
      mfa_code: code,
    });
  }

  // ── Auth ───────────────────────────────────────────────────────────────

  async register(email: string, password: string): Promise<AuthToken> {
    const result = await this.post<AuthToken>("/auth/register", { email, password });
    if (result.access_token) this.token = result.access_token;
    return result;
  }

  async login(email: string, password: string): Promise<AuthToken> {
    const result = await this.post<AuthToken>("/auth/login", { email, password });
    if (result.access_token) this.token = result.access_token;
    return result;
  }

  async me(): Promise<UserProfile> {
    return this.get<UserProfile>("/auth/me");
  }

  // ── Link Flow ──────────────────────────────────────────────────────────

  async createLinkSession(site: string): Promise<LinkSession> {
    return this.post<LinkSession>("/link/create", { site });
  }

  getLinkUrl(linkToken: string): string {
    return `${this.baseUrl}/link?token=${encodeURIComponent(linkToken)}`;
  }

  async registerWebhook(linkToken: string, url: string): Promise<WebhookRegistration> {
    return this.post<WebhookRegistration>("/webhooks", {
      link_token: linkToken,
      url,
    });
  }

  async exchangePublicToken(publicToken: string): Promise<PublicTokenExchangeResult> {
    return this.post<PublicTokenExchangeResult>("/exchange/public_token", {
      public_token: publicToken,
    });
  }

  // ── Links & Tokens ────────────────────────────────────────────────────

  async listLinks(): Promise<{ links: unknown[] }> {
    return this.get("/links");
  }

  async deleteLink(linkToken: string): Promise<{ detail: string }> {
    return this.del(`/links/${encodeURIComponent(linkToken)}`);
  }

  async listTokens(): Promise<{ tokens: unknown[] }> {
    return this.get("/tokens");
  }

  async deleteToken(accessToken: string): Promise<{ detail: string }> {
    return this.del(`/tokens/${encodeURIComponent(accessToken)}`);
  }

  // ── Agents ─────────────────────────────────────────────────────────────

  async registerAgent(
    name: string,
    options?: {
      description?: string;
      allowedScopes?: string[];
      allowedSites?: string[];
      rateLimit?: string;
    },
  ): Promise<AgentInfo> {
    return this.post<AgentInfo>("/agents", {
      name,
      description: options?.description,
      allowed_scopes: options?.allowedScopes,
      allowed_sites: options?.allowedSites,
      rate_limit: options?.rateLimit,
    });
  }

  async listAgents(): Promise<AgentListResult> {
    return this.get<AgentListResult>("/agents");
  }

  async getAgent(agentId: string): Promise<AgentInfo> {
    return this.get<AgentInfo>(`/agents/${encodeURIComponent(agentId)}`);
  }

  async updateAgent(
    agentId: string,
    updates: Partial<Pick<AgentInfo, "name" | "description"> & {
      allowedScopes?: string[];
      allowedSites?: string[];
      rateLimit?: string;
    }>,
  ): Promise<{ status: string; agent_id: string }> {
    return this.patch(`/agents/${encodeURIComponent(agentId)}`, {
      name: updates.name,
      description: updates.description,
      allowed_scopes: updates.allowedScopes,
      allowed_sites: updates.allowedSites,
      rate_limit: updates.rateLimit,
    });
  }

  async deactivateAgent(agentId: string): Promise<{ status: string; agent_id: string }> {
    return this.del(`/agents/${encodeURIComponent(agentId)}`);
  }

  // ── Consent ────────────────────────────────────────────────────────────

  async requestConsent(
    accessToken: string,
    scopes: string[],
    agentName: string,
    durationSeconds = 3600,
  ): Promise<ConsentRequest> {
    return this.post<ConsentRequest>("/consent/request", {
      access_token: accessToken,
      scopes,
      agent_name: agentName,
      duration_seconds: durationSeconds,
    });
  }

  async approveConsent(consentId: number): Promise<ConsentGrant> {
    return this.post<ConsentGrant>(`/consent/${consentId}/approve`);
  }

  async denyConsent(consentId: number): Promise<{ detail: string }> {
    return this.post(`/consent/${consentId}/deny`);
  }

  async listConsents(): Promise<unknown[]> {
    return this.get("/consent");
  }

  async revokeConsent(consentToken: string): Promise<{ detail: string }> {
    return this.del(`/consent/${encodeURIComponent(consentToken)}`);
  }

  // ── API Keys ───────────────────────────────────────────────────────────

  async createApiKey(
    name: string,
    options?: { scopes?: string; expiresDays?: number },
  ): Promise<ApiKeyInfo> {
    return this.post<ApiKeyInfo>("/api-keys", {
      name,
      scopes: options?.scopes,
      expires_days: options?.expiresDays,
    });
  }

  async listApiKeys(): Promise<{ keys: ApiKeyInfo[] }> {
    return this.get("/api-keys");
  }

  async revokeApiKey(keyId: string): Promise<{ detail: string }> {
    return this.del(`/api-keys/${encodeURIComponent(keyId)}`);
  }

  // ── Webhooks (extended) ────────────────────────────────────────────────

  async listWebhooks(): Promise<{ webhooks: unknown[] }> {
    return this.get("/webhooks");
  }

  async deleteWebhook(webhookId: string): Promise<{ detail: string }> {
    return this.del(`/webhooks/${encodeURIComponent(webhookId)}`);
  }

  async testWebhook(webhookId: string): Promise<{ status: string }> {
    return this.post("/webhooks/test", { webhook_id: webhookId });
  }

  async getWebhookDeliveries(webhookId: string): Promise<WebhookDeliveryResult> {
    return this.get<WebhookDeliveryResult>(
      `/webhooks/${encodeURIComponent(webhookId)}/deliveries`,
    );
  }

  // ── Audit ──────────────────────────────────────────────────────────────

  async getAuditLogs(options?: {
    eventType?: string;
    offset?: number;
    limit?: number;
  }): Promise<AuditLogResult> {
    return this.get<AuditLogResult>("/audit/logs", {
      ...(options?.eventType && { event_type: options.eventType }),
      offset: options?.offset ?? 0,
      limit: options?.limit ?? 100,
    });
  }

  async verifyAuditChain(): Promise<AuditVerifyResult> {
    return this.get<AuditVerifyResult>("/audit/verify");
  }

  // ── Scheduled Refresh ──────────────────────────────────────────────────

  async scheduleRefresh(
    accessToken: string,
    intervalSeconds = 3600,
  ): Promise<RefreshScheduleResult> {
    return this.post<RefreshScheduleResult>("/refresh/schedule", {
      access_token: accessToken,
      interval_seconds: intervalSeconds,
    });
  }

  async unscheduleRefresh(accessToken: string): Promise<{ status: string }> {
    return this.del(`/refresh/schedule/${encodeURIComponent(accessToken)}`);
  }

  async listRefreshJobs(): Promise<{ jobs: Record<string, unknown> }> {
    return this.get("/refresh/jobs");
  }

  // ── Fetch Data ─────────────────────────────────────────────────────────

  async fetchData(
    accessToken: string,
    consentToken?: string,
  ): Promise<ConnectResult> {
    const params: Record<string, string> = { access_token: accessToken };
    if (consentToken) params.consent_token = consentToken;
    return this.get<ConnectResult>("/fetch_data", params);
  }
}
