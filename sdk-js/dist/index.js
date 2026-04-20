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
  ServerError: () => ServerError
});
module.exports = __toCommonJS(src_exports);

// src/errors.ts
var PlaidifyError = class extends Error {
  constructor(message, statusCode) {
    super(message);
    this.name = "PlaidifyError";
    this.statusCode = statusCode;
    this.detail = message;
  }
};
var AuthenticationError = class extends PlaidifyError {
  constructor(message = "Authentication failed") {
    super(message, 401);
    this.name = "AuthenticationError";
  }
};
var NotFoundError = class extends PlaidifyError {
  constructor(message = "Resource not found") {
    super(message, 404);
    this.name = "NotFoundError";
  }
};
var RateLimitError = class extends PlaidifyError {
  constructor(message = "Rate limit exceeded") {
    super(message, 429);
    this.name = "RateLimitError";
  }
};
var ServerError = class extends PlaidifyError {
  constructor(message = "Internal server error") {
    super(message, 500);
    this.name = "ServerError";
  }
};

// src/client.ts
async function raiseForStatus(response) {
  if (response.ok) return;
  let detail = `HTTP ${response.status}`;
  try {
    const body = await response.json();
    detail = body.detail || detail;
  } catch {
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
var Plaidify = class {
  constructor(config) {
    this.baseUrl = config.serverUrl.replace(/\/+$/, "");
    this.timeout = config.timeout ?? 3e4;
    this.token = config.token;
    this.apiKey = config.apiKey;
  }
  /** Update the bearer token (e.g. after login). */
  setToken(token) {
    this.token = token;
  }
  // ── HTTP layer ─────────────────────────────────────────────────────────
  headers() {
    const h = { "Content-Type": "application/json" };
    if (this.token) h["Authorization"] = `Bearer ${this.token}`;
    else if (this.apiKey) h["X-API-Key"] = this.apiKey;
    return h;
  }
  async request(method, path, body, params) {
    let url = `${this.baseUrl}${path}`;
    if (params) {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(params)) {
        if (v !== void 0 && v !== null) qs.set(k, String(v));
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
        body: body ? JSON.stringify(body) : void 0,
        signal: controller.signal
      });
      await raiseForStatus(response);
      return await response.json();
    } finally {
      clearTimeout(timer);
    }
  }
  get(path, params) {
    return this.request("GET", path, void 0, params);
  }
  post(path, body) {
    return this.request("POST", path, body);
  }
  patch(path, body) {
    return this.request("PATCH", path, body);
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
  async getBlueprint(name) {
    return this.get(`/blueprints/${encodeURIComponent(name)}`);
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
      mfa_code: code
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
  async register(email, password) {
    const result = await this.post("/auth/register", { email, password });
    if (result.access_token) this.token = result.access_token;
    return result;
  }
  async login(email, password) {
    const result = await this.post("/auth/login", { email, password });
    if (result.access_token) this.token = result.access_token;
    return result;
  }
  async me() {
    return this.get("/auth/me");
  }
  // ── Link Flow ──────────────────────────────────────────────────────────
  async createLinkSession(site) {
    const path = site ? `/link/sessions?site=${encodeURIComponent(site)}` : "/link/sessions";
    return this.post(path);
  }
  async createPublicLinkSession() {
    return this.post("/link/sessions/public");
  }
  getLinkUrl(linkToken, options) {
    const url = new URL(`${this.baseUrl}/link`);
    url.searchParams.set("token", linkToken);
    if (options?.origin) {
      url.searchParams.set("origin", options.origin);
    }
    const theme = options?.theme;
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
  async registerWebhook(linkToken, url) {
    return this.post("/webhooks", {
      link_token: linkToken,
      url
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
  async requestConsent(accessToken, scopes, agentName, durationSeconds = 3600) {
    return this.post("/consent/request", {
      access_token: accessToken,
      scopes,
      agent_name: agentName,
      duration_seconds: durationSeconds
    });
  }
  async approveConsent(consentId) {
    return this.post(`/consent/${consentId}/approve`);
  }
  async denyConsent(consentId) {
    return this.post(`/consent/${consentId}/deny`);
  }
  async listConsents() {
    return this.get("/consent");
  }
  async revokeConsent(consentToken) {
    return this.del(`/consent/${encodeURIComponent(consentToken)}`);
  }
  // ── API Keys ───────────────────────────────────────────────────────────
  async createApiKey(name, options) {
    return this.post("/api-keys", {
      name,
      scopes: options?.scopes,
      expires_days: options?.expiresDays
    });
  }
  async listApiKeys() {
    return this.get("/api-keys");
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
  async scheduleRefresh(accessToken, intervalSeconds = 3600) {
    return this.post("/refresh/schedule", {
      access_token: accessToken,
      interval_seconds: intervalSeconds
    });
  }
  async unscheduleRefresh(accessToken) {
    return this.del(`/refresh/schedule/${encodeURIComponent(accessToken)}`);
  }
  async listRefreshJobs() {
    return this.get("/refresh/jobs");
  }
  // ── Fetch Data ─────────────────────────────────────────────────────────
  async fetchData(accessToken, consentToken) {
    const params = { access_token: accessToken };
    if (consentToken) params.consent_token = consentToken;
    return this.get("/fetch_data", params);
  }
};
// Annotate the CommonJS export names for ESM import in node:
0 && (module.exports = {
  AuthenticationError,
  NotFoundError,
  Plaidify,
  PlaidifyError,
  RateLimitError,
  ServerError
});
//# sourceMappingURL=index.js.map