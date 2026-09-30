/**
 * Plaidify TypeScript/JavaScript API client.
 *
 * @example
 * ```ts
 * import { Plaidify } from "@plaidify/client";
 *
 * const pfy = new Plaidify({ serverUrl: "http://localhost:8000" });
 * await pfy.login("alice", "password");
 * const blueprints = await pfy.listBlueprints();
 * ```
 */

import type {
  AccessJob,
  AccessJobListResult,
  AccessTokenInfo,
  PlaidifyConfig,
  HealthStatus,
  BlueprintInfo,
  BlueprintListResult,
  ConnectResult,
  AuthToken,
  UserProfile,
  LinkInfo,
  LinkSession,
  MfaSubmitResult,
  WebhookRegistration,
  WebhookListResult,
  AgentInfo,
  AgentListResult,
  ConsentRequest,
  ConsentGrant,
  ConsentListResult,
  ApiKeyCreated,
  ApiKeyInfo,
  AuditLogResult,
  AuditVerifyResult,
  WebhookDeliveryResult,
  PublicTokenExchangeResult,
  RefreshJobListResult,
  RefreshScheduleResult,
  HostedLinkBootstrapRequest,
  HostedLinkBootstrapResponse,
  HostedLinkUrlOptions,
  PlaidifyErrorResponse,
} from "./types";

import {
  PlaidifyError,
  AuthenticationError,
  NotFoundError,
  RateLimitError,
  ServerError,
} from "./errors";
import { buildHostedLinkUrl } from "./link-url";

// ── HTTP helpers ─────────────────────────────────────────────────────────────

/** API keys (`pk_…`, agents `pk_agent_…`) authenticate with X-API-Key. */
function isApiKey(credential: string): boolean {
  return credential.startsWith("pk_");
}

/**
 * Human-readable message from an error body: FastAPI's `detail` (a string,
 * or a list of validation issues) or Plaidify's `{error, error_code}`.
 */
function errorMessage(body: PlaidifyErrorResponse | null, status: number): string {
  const detail = body?.detail;
  if (typeof detail === "string" && detail) {
    return detail;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    return detail
      .map((issue) => (issue && typeof issue.msg === "string" ? issue.msg : JSON.stringify(issue)))
      .join("; ");
  }
  if (typeof body?.error === "string" && body.error) {
    return body.error;
  }
  return `HTTP ${status}`;
}

async function raiseForStatus(response: Response): Promise<void> {
  if (response.ok) return;

  let body: PlaidifyErrorResponse | null = null;
  try {
    body = (await response.json()) as PlaidifyErrorResponse;
  } catch {
    // no JSON body
  }
  const detail = errorMessage(body, response.status);
  const errorCode = typeof body?.error_code === "string" ? body.error_code : undefined;

  switch (response.status) {
    case 401:
      throw new AuthenticationError(detail, errorCode);
    case 404:
      throw new NotFoundError(detail, errorCode);
    case 429:
      throw new RateLimitError(detail, errorCode);
    default:
      if (response.status >= 500) throw new ServerError(detail, errorCode);
      throw new PlaidifyError(detail, response.status, errorCode);
  }
}

/** GET /api-keys returns scopes as the stored JSON text; hand back a list. */
function parseScopes(raw: unknown): string[] | null {
  if (Array.isArray(raw)) {
    return raw.filter((scope): scope is string => typeof scope === "string");
  }
  if (typeof raw === "string" && raw) {
    try {
      return parseScopes(JSON.parse(raw));
    } catch {
      return null;
    }
  }
  return null;
}

interface RequestOptions {
  /** Sent as a JSON body. */
  json?: unknown;
  /** Sent as application/x-www-form-urlencoded. */
  form?: Record<string, string>;
  /** Query parameters — never for secrets, which belong in the body. */
  params?: Record<string, string | number>;
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

  private authHeaders(): Record<string, string> {
    // A user token wins over an API key when both are configured. Either
    // way an API key only ever travels in X-API-Key: the server does not
    // accept one as a bearer token.
    const credential = this.token ?? this.apiKey;
    if (!credential) {
      return {};
    }
    if (credential === this.apiKey || isApiKey(credential)) {
      return { "X-API-Key": credential };
    }
    return { Authorization: `Bearer ${credential}` };
  }

  private async request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
    let url = `${this.baseUrl}${path}`;
    if (options.params) {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(options.params)) {
        if (v !== undefined && v !== null) qs.set(k, String(v));
      }
      const qsStr = qs.toString();
      if (qsStr) url += `?${qsStr}`;
    }

    const headers: Record<string, string> = { Accept: "application/json", ...this.authHeaders() };
    let body: string | undefined;
    if (options.form) {
      headers["Content-Type"] = "application/x-www-form-urlencoded";
      body = new URLSearchParams(options.form).toString();
    } else if (options.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.json);
    }

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);

    try {
      const response = await fetch(url, {
        method,
        headers,
        body,
        signal: controller.signal,
      });
      await raiseForStatus(response);
      return (await response.json()) as T;
    } finally {
      clearTimeout(timer);
    }
  }

  private get<T>(path: string, params?: Record<string, string | number>): Promise<T> {
    return this.request<T>("GET", path, { params });
  }

  private post<T>(path: string, json?: unknown): Promise<T> {
    return this.request<T>("POST", path, { json });
  }

  private patch<T>(path: string, json?: unknown): Promise<T> {
    return this.request<T>("PATCH", path, { json });
  }

  private del<T>(path: string): Promise<T> {
    return this.request<T>("DELETE", path);
  }

  private async sleep(ms: number): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, ms));
  }

  // ── Health ─────────────────────────────────────────────────────────────

  async health(): Promise<HealthStatus> {
    return this.get<HealthStatus>("/health");
  }

  // ── Blueprints ─────────────────────────────────────────────────────────

  async listBlueprints(): Promise<BlueprintListResult> {
    return this.get<BlueprintListResult>("/blueprints");
  }

  async getBlueprint(site: string): Promise<BlueprintInfo> {
    return this.get<BlueprintInfo>(`/blueprints/${encodeURIComponent(site)}`);
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

  async submitMfa(sessionId: string, code: string): Promise<MfaSubmitResult> {
    return this.post<MfaSubmitResult>("/mfa/submit", {
      session_id: sessionId,
      code,
    });
  }

  async listAccessJobs(options?: {
    limit?: number;
    site?: string;
    status?: string;
    jobType?: string;
  }): Promise<AccessJobListResult> {
    return this.get<AccessJobListResult>("/access_jobs", {
      limit: options?.limit ?? 20,
      ...(options?.site && { site: options.site }),
      ...(options?.status && { status: options.status }),
      ...(options?.jobType && { job_type: options.jobType }),
    });
  }

  async getAccessJob(jobId: string): Promise<AccessJob> {
    return this.get<AccessJob>(`/access_jobs/${encodeURIComponent(jobId)}`);
  }

  async waitForAccessJob(
    jobId: string,
    options?: { pollIntervalMs?: number; timeoutMs?: number },
  ): Promise<AccessJob> {
    const pollIntervalMs = options?.pollIntervalMs ?? 500;
    const timeoutMs = options?.timeoutMs ?? 30_000;
    const deadline = Date.now() + timeoutMs;

    while (true) {
      const job = await this.getAccessJob(jobId);
      if (job.status !== "pending" && job.status !== "running") {
        return job;
      }

      if (Date.now() >= deadline) {
        throw new PlaidifyError(`Timed out waiting for access job: ${jobId}`, 408);
      }

      await this.sleep(Math.min(pollIntervalMs, Math.max(deadline - Date.now(), 0)));
    }
  }

  // ── Auth ───────────────────────────────────────────────────────────────

  /** Create an account and use its access token for later calls. */
  async register(username: string, email: string, password: string): Promise<AuthToken> {
    const result = await this.post<AuthToken>("/auth/register", { username, email, password });
    if (result.access_token) this.token = result.access_token;
    return result;
  }

  /** Log in (OAuth2 password form at POST /auth/token) and keep the token. */
  async login(username: string, password: string): Promise<AuthToken> {
    const result = await this.request<AuthToken>("POST", "/auth/token", {
      form: { username, password },
    });
    if (result.access_token) this.token = result.access_token;
    return result;
  }

  async me(): Promise<UserProfile> {
    return this.get<UserProfile>("/auth/me");
  }

  // ── Link Flow ──────────────────────────────────────────────────────────

  async createLinkSession(site?: string): Promise<LinkSession> {
    return this.request<LinkSession>("POST", "/link/sessions", {
      params: site ? { site } : undefined,
    });
  }

  async createPublicLinkSession(): Promise<LinkSession> {
    return this.post<LinkSession>("/link/sessions/public");
  }

  async createHostedLinkBootstrap(
    options?: HostedLinkBootstrapRequest,
  ): Promise<HostedLinkBootstrapResponse> {
    return this.post<HostedLinkBootstrapResponse>("/link/bootstrap", {
      site: options?.site,
      allowed_origin: options?.allowedOrigin,
      allowed_origins: options?.allowedOrigins,
      scopes: options?.scopes,
    });
  }

  async exchangeHostedLinkBootstrap(launchToken: string): Promise<LinkSession> {
    return this.post<LinkSession>("/link/sessions/bootstrap", {
      launch_token: launchToken,
    });
  }

  getLinkUrl(linkToken: string, options?: HostedLinkUrlOptions): string {
    return buildHostedLinkUrl(this.baseUrl, linkToken, options);
  }

  /**
   * Register a webhook for a link session. Each delivery carries
   * `X-Plaidify-Delivery` (the same id on every retry, for de-duplication),
   * `X-Plaidify-Timestamp` (Unix seconds) and `X-Plaidify-Signature`:
   * `sha256=` + hex HMAC-SHA256, keyed with `secret`, of
   * `` `${timestamp}.${rawBody}` ``. Recompute it over the raw body and reject
   * timestamps older than a few minutes.
   */
  async registerWebhook(linkToken: string, url: string, secret: string): Promise<WebhookRegistration> {
    return this.post<WebhookRegistration>("/webhooks/register", {
      link_token: linkToken,
      url,
      secret,
    });
  }

  async exchangePublicToken(publicToken: string): Promise<PublicTokenExchangeResult> {
    return this.post<PublicTokenExchangeResult>("/exchange/public_token", {
      public_token: publicToken,
    });
  }

  // ── Links & Tokens ────────────────────────────────────────────────────

  async listLinks(): Promise<LinkInfo[]> {
    return this.get<LinkInfo[]>("/links");
  }

  async deleteLink(linkToken: string): Promise<{ status: string }> {
    return this.del(`/links/${encodeURIComponent(linkToken)}`);
  }

  async listTokens(): Promise<AccessTokenInfo[]> {
    return this.get<AccessTokenInfo[]>("/tokens");
  }

  async deleteToken(accessToken: string): Promise<{ status: string }> {
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
    updates: {
      name?: string;
      description?: string;
      allowedScopes?: string[];
      allowedSites?: string[];
      rateLimit?: string;
    },
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
    options?: { agentDescription?: string },
  ): Promise<ConsentRequest> {
    return this.post<ConsentRequest>("/consent/request", {
      access_token: accessToken,
      scopes,
      agent_name: agentName,
      agent_description: options?.agentDescription,
      duration_seconds: durationSeconds,
    });
  }

  /** `requestId` is the `request_id` returned by {@link requestConsent}. */
  async approveConsent(requestId: string): Promise<ConsentGrant> {
    return this.post<ConsentGrant>(`/consent/${encodeURIComponent(requestId)}/approve`);
  }

  async denyConsent(requestId: string): Promise<{ request_id: string; status: string }> {
    return this.post(`/consent/${encodeURIComponent(requestId)}/deny`);
  }

  async listConsents(): Promise<ConsentListResult> {
    return this.get<ConsentListResult>("/consent");
  }

  async revokeConsent(consentToken: string): Promise<{ status: string; consent_token: string }> {
    return this.del(`/consent/${encodeURIComponent(consentToken)}`);
  }

  // ── API Keys ───────────────────────────────────────────────────────────

  /** The raw key is only in this response (`key`) — store it now. */
  async createApiKey(
    name: string,
    options?: { scopes?: string[]; expiresDays?: number },
  ): Promise<ApiKeyCreated> {
    return this.post<ApiKeyCreated>("/api-keys", {
      name,
      scopes: options?.scopes,
      expires_days: options?.expiresDays,
    });
  }

  async listApiKeys(): Promise<ApiKeyInfo[]> {
    const keys = await this.get<(Omit<ApiKeyInfo, "scopes"> & { scopes: unknown })[]>("/api-keys");
    return keys.map((key) => ({ ...key, scopes: parseScopes(key.scopes) }));
  }

  async revokeApiKey(keyId: string): Promise<{ status: string; key_id: string }> {
    return this.del(`/api-keys/${encodeURIComponent(keyId)}`);
  }

  // ── Webhooks (extended) ────────────────────────────────────────────────

  async listWebhooks(): Promise<WebhookListResult> {
    return this.get<WebhookListResult>("/webhooks");
  }

  async deleteWebhook(webhookId: string): Promise<{ status: string }> {
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
    options?: { scheduleFormat?: string },
  ): Promise<RefreshScheduleResult> {
    return this.post<RefreshScheduleResult>("/refresh/schedule", {
      access_token: accessToken,
      interval_seconds: intervalSeconds,
      schedule_format: options?.scheduleFormat,
    });
  }

  async unscheduleRefresh(accessToken: string): Promise<{ status: string; access_token: string }> {
    return this.del(`/refresh/schedule/${encodeURIComponent(accessToken)}`);
  }

  /** The caller's own schedules, tokens masked. */
  async listRefreshJobs(): Promise<RefreshJobListResult> {
    return this.get<RefreshJobListResult>("/refresh/jobs");
  }

  // ── Fetch Data ─────────────────────────────────────────────────────────

  /** Tokens go in the body: a query string would land in access logs. */
  async fetchData(accessToken: string, consentToken?: string): Promise<ConnectResult> {
    return this.post<ConnectResult>("/fetch_data", {
      access_token: accessToken,
      consent_token: consentToken,
    });
  }
}
