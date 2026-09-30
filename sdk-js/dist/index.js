"use strict";
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// src/index.ts
var src_exports = {};
__export(src_exports, {
  AuthenticationError: () => AuthenticationError,
  NotFoundError: () => NotFoundError,
  Plaidify: () => Plaidify,
  PlaidifyError: () => PlaidifyError,
  RateLimitError: () => RateLimitError,
  ServerError: () => ServerError,
  buildHostedLinkUrl: () => buildHostedLinkUrl
});
module.exports = __toCommonJS(src_exports);

// src/errors.ts
var PlaidifyError = class extends Error {
  constructor(message, statusCode, errorCode) {
    super(message);
    this.name = "PlaidifyError";
    this.statusCode = statusCode;
    this.detail = message;
    this.errorCode = errorCode;
  }
};
var AuthenticationError = class extends PlaidifyError {
  constructor(message = "Authentication failed", errorCode) {
    super(message, 401, errorCode);
    this.name = "AuthenticationError";
  }
};
var NotFoundError = class extends PlaidifyError {
  constructor(message = "Resource not found", errorCode) {
    super(message, 404, errorCode);
    this.name = "NotFoundError";
  }
};
var RateLimitError = class extends PlaidifyError {
  constructor(message = "Rate limit exceeded", errorCode) {
    super(message, 429, errorCode);
    this.name = "RateLimitError";
  }
};
var ServerError = class extends PlaidifyError {
  constructor(message = "Internal server error", errorCode) {
    super(message, 500, errorCode);
    this.name = "ServerError";
  }
};

// src/link-url.ts
function trimTrailingSlashes(url) {
  let end = url.length;
  while (end > 0 && url.charCodeAt(end - 1) === 47) {
    end -= 1;
  }
  return url.slice(0, end);
}
function buildHostedLinkUrl(serverUrl, linkToken, options = {}, base) {
  const url = new URL(`${trimTrailingSlashes(serverUrl)}/link`, base);
  url.searchParams.set("token", linkToken);
  if (options.origin) {
    url.searchParams.set("origin", options.origin);
  }
  const theme = options.theme;
  if (theme?.accentColor) {
    url.searchParams.set("accent", theme.accentColor);
  }
  if (theme?.bgColor) {
    url.searchParams.set("bg", theme.bgColor);
  }
  if (theme?.borderRadius) {
    url.searchParams.set("radius", theme.borderRadius);
  }
  if (theme?.logo) {
    url.searchParams.set("logo", theme.logo);
  }
  return url.toString();
}

// src/client.ts
function isApiKey(credential) {
  return credential.startsWith("pk_");
}
function errorMessage(body, status) {
  const detail = body?.detail;
  if (typeof detail === "string" && detail) {
    return detail;
  }
  if (Array.isArray(detail) && detail.length > 0) {
    return detail.map((issue) => issue && typeof issue.msg === "string" ? issue.msg : JSON.stringify(issue)).join("; ");
  }
  if (typeof body?.error === "string" && body.error) {
    return body.error;
  }
  return `HTTP ${status}`;
}
async function raiseForStatus(response) {
  if (response.ok) return;
  let body = null;
  try {
    body = await response.json();
  } catch {
  }
  const detail = errorMessage(body, response.status);
  const errorCode = typeof body?.error_code === "string" ? body.error_code : void 0;
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
function parseScopes(raw) {
  if (Array.isArray(raw)) {
    return raw.filter((scope) => typeof scope === "string");
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
var Plaidify = class {
  constructor(config) {
    this.baseUrl = trimTrailingSlashes(config.serverUrl);
    this.timeout = config.timeout ?? 3e4;
    this.token = config.token;
    this.apiKey = config.apiKey;
  }
  /** Update the bearer token (e.g. after login). */
  setToken(token) {
    this.token = token;
  }
  // ── HTTP layer ─────────────────────────────────────────────────────────
  authHeaders() {
    const credential = this.token ?? this.apiKey;
    if (!credential) {
      return {};
    }
    if (credential === this.apiKey || isApiKey(credential)) {
      return { "X-API-Key": credential };
    }
    return { Authorization: `Bearer ${credential}` };
  }
  async request(method, path, options = {}) {
    let url = `${this.baseUrl}${path}`;
    if (options.params) {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(options.params)) {
        if (v !== void 0 && v !== null) qs.set(k, String(v));
      }
      const qsStr = qs.toString();
      if (qsStr) url += `?${qsStr}`;
    }
    const headers = { Accept: "application/json", ...this.authHeaders() };
    let body;
    if (options.form) {
      headers["Content-Type"] = "application/x-www-form-urlencoded";
      body = new URLSearchParams(options.form).toString();
    } else if (options.json !== void 0) {
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
        signal: controller.signal
      });
      await raiseForStatus(response);
      return await response.json();
    } finally {
      clearTimeout(timer);
    }
  }
  get(path, params) {
    return this.request("GET", path, { params });
  }
  post(path, json) {
    return this.request("POST", path, { json });
  }
  patch(path, json) {
    return this.request("PATCH", path, { json });
  }
  del(path) {
    return this.request("DELETE", path);
  }
  async sleep(ms) {
    await new Promise((resolve) => setTimeout(resolve, ms));
  }
  // ── Health ─────────────────────────────────────────────────────────────
  async health() {
    return this.get("/health");
  }
  // ── Blueprints ─────────────────────────────────────────────────────────
  async listBlueprints() {
    return this.get("/blueprints");
  }
  async getBlueprint(site) {
    return this.get(`/blueprints/${encodeURIComponent(site)}`);
  }
  // ── Connect ────────────────────────────────────────────────────────────
  async connect(site, username, password, options) {
    return this.post("/connect", {
      site,
      username,
      password,
      extract_fields: options?.extractFields
    });
  }
  async submitMfa(sessionId, code) {
    return this.post("/mfa/submit", {
      session_id: sessionId,
      code
    });
  }
  async listAccessJobs(options) {
    return this.get("/access_jobs", {
      limit: options?.limit ?? 20,
      ...options?.site && { site: options.site },
      ...options?.status && { status: options.status },
      ...options?.jobType && { job_type: options.jobType }
    });
  }
  async getAccessJob(jobId) {
    return this.get(`/access_jobs/${encodeURIComponent(jobId)}`);
  }
  async waitForAccessJob(jobId, options) {
    const pollIntervalMs = options?.pollIntervalMs ?? 500;
    const timeoutMs = options?.timeoutMs ?? 3e4;
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
  async register(username, email, password) {
    const result = await this.post("/auth/register", { username, email, password });
    if (result.access_token) this.token = result.access_token;
    return result;
  }
  /** Log in (OAuth2 password form at POST /auth/token) and keep the token. */
  async login(username, password) {
    const result = await this.request("POST", "/auth/token", {
      form: { username, password }
    });
    if (result.access_token) this.token = result.access_token;
    return result;
  }
  async me() {
    return this.get("/auth/me");
  }
  // ── Link Flow ──────────────────────────────────────────────────────────
  async createLinkSession(site) {
    return this.request("POST", "/link/sessions", {
      params: site ? { site } : void 0
    });
  }
  async createPublicLinkSession() {
    return this.post("/link/sessions/public");
  }
  async createHostedLinkBootstrap(options) {
    return this.post("/link/bootstrap", {
      site: options?.site,
      allowed_origin: options?.allowedOrigin,
      allowed_origins: options?.allowedOrigins,
      scopes: options?.scopes
    });
  }
  async exchangeHostedLinkBootstrap(launchToken) {
    return this.post("/link/sessions/bootstrap", {
      launch_token: launchToken
    });
  }
  getLinkUrl(linkToken, options) {
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
  async registerWebhook(linkToken, url, secret) {
    return this.post("/webhooks/register", {
      link_token: linkToken,
      url,
      secret
    });
  }
  async exchangePublicToken(publicToken) {
    return this.post("/exchange/public_token", {
      public_token: publicToken
    });
  }
  // ── Links & Tokens ────────────────────────────────────────────────────
  async listLinks() {
    return this.get("/links");
  }
  async deleteLink(linkToken) {
    return this.del(`/links/${encodeURIComponent(linkToken)}`);
  }
  async listTokens() {
    return this.get("/tokens");
  }
  async deleteToken(accessToken) {
    return this.del(`/tokens/${encodeURIComponent(accessToken)}`);
  }
  // ── Agents ─────────────────────────────────────────────────────────────
  async registerAgent(name, options) {
    return this.post("/agents", {
      name,
      description: options?.description,
      allowed_scopes: options?.allowedScopes,
      allowed_sites: options?.allowedSites,
      rate_limit: options?.rateLimit
    });
  }
  async listAgents() {
    return this.get("/agents");
  }
  async getAgent(agentId) {
    return this.get(`/agents/${encodeURIComponent(agentId)}`);
  }
  async updateAgent(agentId, updates) {
    return this.patch(`/agents/${encodeURIComponent(agentId)}`, {
      name: updates.name,
      description: updates.description,
      allowed_scopes: updates.allowedScopes,
      allowed_sites: updates.allowedSites,
      rate_limit: updates.rateLimit
    });
  }
  async deactivateAgent(agentId) {
    return this.del(`/agents/${encodeURIComponent(agentId)}`);
  }
  // ── Consent ────────────────────────────────────────────────────────────
  async requestConsent(accessToken, scopes, agentName, durationSeconds = 3600, options) {
    return this.post("/consent/request", {
      access_token: accessToken,
      scopes,
      agent_name: agentName,
      agent_description: options?.agentDescription,
      duration_seconds: durationSeconds
    });
  }
  /** `requestId` is the `request_id` returned by {@link requestConsent}. */
  async approveConsent(requestId) {
    return this.post(`/consent/${encodeURIComponent(requestId)}/approve`);
  }
  async denyConsent(requestId) {
    return this.post(`/consent/${encodeURIComponent(requestId)}/deny`);
  }
  async listConsents() {
    return this.get("/consent");
  }
  async revokeConsent(consentToken) {
    return this.del(`/consent/${encodeURIComponent(consentToken)}`);
  }
  // ── API Keys ───────────────────────────────────────────────────────────
  /** The raw key is only in this response (`key`) — store it now. */
  async createApiKey(name, options) {
    return this.post("/api-keys", {
      name,
      scopes: options?.scopes,
      expires_days: options?.expiresDays
    });
  }
  async listApiKeys() {
    const keys = await this.get("/api-keys");
    return keys.map((key) => ({ ...key, scopes: parseScopes(key.scopes) }));
  }
  async revokeApiKey(keyId) {
    return this.del(`/api-keys/${encodeURIComponent(keyId)}`);
  }
  // ── Webhooks (extended) ────────────────────────────────────────────────
  async listWebhooks() {
    return this.get("/webhooks");
  }
  async deleteWebhook(webhookId) {
    return this.del(`/webhooks/${encodeURIComponent(webhookId)}`);
  }
  async testWebhook(webhookId) {
    return this.post("/webhooks/test", { webhook_id: webhookId });
  }
  async getWebhookDeliveries(webhookId) {
    return this.get(
      `/webhooks/${encodeURIComponent(webhookId)}/deliveries`
    );
  }
  // ── Audit ──────────────────────────────────────────────────────────────
  async getAuditLogs(options) {
    return this.get("/audit/logs", {
      ...options?.eventType && { event_type: options.eventType },
      offset: options?.offset ?? 0,
      limit: options?.limit ?? 100
    });
  }
  async verifyAuditChain() {
    return this.get("/audit/verify");
  }
  // ── Scheduled Refresh ──────────────────────────────────────────────────
  async scheduleRefresh(accessToken, intervalSeconds = 3600, options) {
    return this.post("/refresh/schedule", {
      access_token: accessToken,
      interval_seconds: intervalSeconds,
      schedule_format: options?.scheduleFormat
    });
  }
  async unscheduleRefresh(accessToken) {
    return this.del(`/refresh/schedule/${encodeURIComponent(accessToken)}`);
  }
  /** The caller's own schedules, tokens masked. */
  async listRefreshJobs() {
    return this.get("/refresh/jobs");
  }
  // ── Fetch Data ─────────────────────────────────────────────────────────
  /** Tokens go in the body: a query string would land in access logs. */
  async fetchData(accessToken, consentToken) {
    return this.post("/fetch_data", {
      access_token: accessToken,
      consent_token: consentToken
    });
  }
};
// Annotate the CommonJS export names for ESM import in node:
0 && (module.exports = {
  AuthenticationError,
  NotFoundError,
  Plaidify,
  PlaidifyError,
  RateLimitError,
  ServerError,
  buildHostedLinkUrl
});
//# sourceMappingURL=index.js.map