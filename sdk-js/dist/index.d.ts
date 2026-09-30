/**
 * Plaidify SDK type definitions.
 *
 * Response types mirror what the server actually returns (src/routers/*.py
 * and src/models.py); fields the server does not send are not declared.
 */
interface PlaidifyConfig {
    /** Base URL of the Plaidify server (e.g. "http://localhost:8000"). */
    serverUrl: string;
    /** User access token (JWT), sent as `Authorization: Bearer`. */
    token?: string;
    /** API key (`pk_…` / `pk_agent_…`), sent as `X-API-Key`. */
    apiKey?: string;
    /** Request timeout in milliseconds (default: 30000). */
    timeout?: number;
}
/** GET /health */
interface HealthStatus {
    status: string;
}
/** An entry of GET /blueprints. */
interface BlueprintSummary {
    site: string;
    name: string;
    domain: string;
    tags: string[];
    has_mfa: boolean;
    schema_version: string;
}
/** GET /blueprints/{site} */
interface BlueprintInfo {
    name: string;
    domain: string;
    tags: string[];
    has_mfa: boolean;
    extract_fields: string[];
    schema_version: string;
    rate_limit?: Record<string, unknown> | null;
}
interface BlueprintListResult {
    blueprints: BlueprintSummary[];
    count: number;
}
/** POST /connect, GET/POST /fetch_data */
interface ConnectResult {
    status: string;
    job_id?: string | null;
    data?: Record<string, unknown> | null;
    session_id?: string | null;
    mfa_type?: string | null;
    metadata?: Record<string, unknown> | null;
    /** fetch_data only: instructions stored for the access token. */
    instructions_applied?: string;
    /** fetch_data only: the fields the consent/token scopes allowed. */
    scopes_applied?: string[];
}
/** POST /mfa/submit — "mfa_submitted", or "error" with `error`. */
interface MfaSubmitResult {
    status: string;
    message?: string;
    error?: string;
}
interface AccessJob {
    job_id: string;
    site: string;
    job_type: string;
    status: string;
    session_id?: string | null;
    mfa_type?: string | null;
    error_message?: string | null;
    metadata?: Record<string, unknown> | null;
    result?: Record<string, unknown> | null;
    created_at?: string | null;
    started_at?: string | null;
    completed_at?: string | null;
}
interface AccessJobListResult {
    jobs: AccessJob[];
    count: number;
}
interface MFAChallenge {
    session_id: string;
    mfa_type: string;
    prompt?: string;
}
/** POST /auth/register, POST /auth/verify-email, POST /auth/token */
interface AuthToken {
    access_token: string;
    refresh_token?: string | null;
    token_type: string;
}
/**
 * POST /auth/register (202) while the server has sign-ups prove their email
 * address first. The same whether or not the username or address was free;
 * finish with `verifyEmail(token)`, the token coming from the email.
 */
interface RegistrationPending {
    status: "verification_sent";
    detail: string;
}
/** GET /auth/me */
interface UserProfile {
    id: number;
    username: string | null;
    email: string | null;
    is_active: boolean;
}
/** POST /link/sessions, /link/sessions/public, /link/sessions/bootstrap */
interface LinkSession {
    link_token: string;
    link_url: string;
    public_key?: string;
    expires_in: number;
    scopes?: string[] | null;
}
/** An entry of GET /links. */
interface LinkInfo {
    link_token: string;
    site: string;
}
/** An entry of GET /tokens. */
interface AccessTokenInfo {
    token: string;
    link_token: string;
}
interface HostedLinkUrlOptions {
    /** Origin of the page embedding Link (web embeds only). */
    origin?: string;
    theme?: LinkTheme;
}
interface HostedLinkBootstrapRequest {
    site?: string;
    allowedOrigin?: string;
    /** Every origin allowed to embed the session (up to 20). */
    allowedOrigins?: string[];
    scopes?: string[];
}
interface HostedLinkBootstrapResponse {
    launch_token: string;
    expires_in: number;
    site?: string | null;
    allowed_origin?: string | null;
    allowed_origins?: string[] | null;
    scopes?: string[] | null;
}
type PlaidifyLinkEventName = "OPEN" | "CLOSE" | "INSTITUTION_SELECTED" | "CREDENTIALS_SUBMITTED" | "MFA_REQUIRED" | "MFA_SUBMITTED" | "CONNECTED" | "ERROR" | "EXIT" | "DONE" | "TELEMETRY" | "SUPPORT_REQUESTED";
interface PlaidifyLinkMfaDetails {
    mfa_type?: string;
    session_id?: string;
}
interface PlaidifyLinkExitDetails {
    reason?: string;
    error?: string;
    /** Error-taxonomy code of the last error, when the user exits from one. */
    error_code?: string;
}
interface PlaidifyLinkSuccessMetadata {
    job_id?: string;
    organization_id?: string;
    organization_name?: string;
    public_token?: string;
    site?: string;
}
interface PlaidifyLinkEventPayload extends PlaidifyLinkExitDetails, PlaidifyLinkMfaDetails {
    source?: "plaidify-link";
    event?: PlaidifyLinkEventName | string;
    job_id?: string;
    public_token?: string;
    organization_id?: string;
    organization_name?: string;
    site?: string;
    /** TELEMETRY only: which telemetry event (step_view, field_error, …). */
    name?: string;
    /** TELEMETRY only: the step the event concerns. */
    step?: string;
    /** TELEMETRY only: the form field that failed validation (never its value). */
    field?: string;
    /** TELEMETRY only: milliseconds since Link opened. */
    elapsed_ms?: number;
}
/** An SSE event from GET /link/events/{link_token}. */
interface LinkEvent {
    event: string;
    timestamp: string;
    data?: Record<string, unknown>;
}
/** POST /webhooks/register */
interface WebhookRegistration {
    webhook_id: string;
    status: string;
}
/** An entry of GET /webhooks. */
interface WebhookInfo {
    webhook_id: string;
    link_token: string;
    url: string;
    created_at: string | null;
}
interface WebhookListResult {
    webhooks: WebhookInfo[];
    count: number;
}
interface AgentInfo {
    agent_id: string;
    name: string;
    description?: string | null;
    /** Only in the POST /agents response — store it, it is not shown again. */
    api_key?: string;
    api_key_prefix?: string;
    allowed_scopes?: string[] | null;
    allowed_sites?: string[] | null;
    rate_limit?: string | null;
    is_active?: boolean;
    last_active_at?: string | null;
    created_at?: string | null;
}
interface AgentListResult {
    agents: AgentInfo[];
    count: number;
}
/** POST /consent/request */
interface ConsentRequest {
    request_id: string;
    agent_name: string;
    scopes: string[];
    duration_seconds: number;
    status: string;
}
/** POST /consent/{request_id}/approve */
interface ConsentGrant {
    consent_token: string;
    scopes: string[];
    expires_at: string;
    status: string;
}
/** An entry of GET /consent. */
interface ConsentGrantInfo {
    consent_token: string;
    agent_name: string;
    scopes: string[];
    access_token: string;
    expires_at: string;
    created_at: string | null;
}
interface ConsentListResult {
    grants: ConsentGrantInfo[];
    count: number;
}
/** POST /api-keys — the only response that carries the raw key. */
interface ApiKeyCreated {
    id: string;
    name: string;
    key: string;
    key_prefix: string;
    expires_at: string | null;
    created_at: string | null;
}
/** An entry of GET /api-keys. */
interface ApiKeyInfo {
    id: string;
    name: string;
    key_prefix: string;
    /** Scopes the key is limited to; null means every scope. */
    scopes: string[] | null;
    expires_at: string | null;
    last_used_at: string | null;
    created_at: string | null;
}
interface AuditEntry {
    id: number;
    event_type: string;
    action: string;
    user_id?: number | null;
    agent_id?: string | null;
    resource?: string | null;
    metadata?: Record<string, unknown> | null;
    ip_address?: string | null;
    timestamp?: string;
    entry_hash?: string;
}
interface AuditLogResult {
    entries: AuditEntry[];
    total: number;
    offset: number;
    limit: number;
}
interface AuditChainError {
    id: number;
    error: string;
    expected?: string | null;
    actual?: string | null;
}
interface AuditVerifyResult {
    valid: boolean;
    total: number;
    errors: AuditChainError[];
}
/** One delivery attempt recorded for a webhook. */
interface WebhookDelivery {
    attempt: number;
    success: boolean;
    timestamp: number;
    status_code?: number;
    error?: string;
}
interface WebhookDeliveryResult {
    webhook_id: string;
    url: string;
    deliveries: WebhookDelivery[];
    total: number;
}
interface PublicTokenExchangeResult {
    access_token: string;
}
interface RefreshScheduleResult {
    status: string;
    /** Truncated for display, e.g. "3f2a9c1d-8e4b...". */
    access_token: string;
    interval_seconds: number;
    schedule_format: string;
}
interface RefreshJobInfo {
    /** Truncated for display, e.g. "3f2a9c1d-8e4b...". */
    access_token: string;
    interval_seconds: number;
    schedule_format: string;
    enabled: boolean;
    /** Why the schedule stopped, e.g. "needs_reauth" once the site asked for MFA. */
    disabled_reason: string | null;
    last_refreshed: string | null;
    next_run_at: string | null;
    last_error: string | null;
    consecutive_failures: number;
}
interface RefreshJobListResult {
    jobs: RefreshJobInfo[];
}
interface PlaidifyLinkConfig {
    /** Plaidify server URL. */
    serverUrl: string;
    /** Link token from POST /link/sessions or POST /link/sessions/public. */
    token: string;
    /** Theme overrides for the link UI. */
    theme?: LinkTheme;
    /** Called when link completes successfully with a public token, when one exists. */
    onSuccess?: (publicToken: string, metadata: PlaidifyLinkSuccessMetadata) => void;
    /** Called once when the user leaves Link without connecting. */
    onExit?: (details: PlaidifyLinkExitDetails) => void;
    /** Called on each link event, including recoverable ERRORs. */
    onEvent?: (event: PlaidifyLinkEventName | string, data: PlaidifyLinkEventPayload) => void;
    /** Called when the provider requires additional verification. */
    onMFA?: (details: PlaidifyLinkMfaDetails) => void;
}
interface LinkTheme {
    /** Buttons and focus rings, as a hex colour ("#0b8f73"). */
    accentColor?: string;
    /** Page background behind the Link card, as a hex colour. */
    bgColor?: string;
    /** Corner radius of the Link card, e.g. "24px" or "1.5rem". */
    borderRadius?: string;
    /**
     * Logo shown above every step, as a `data:image/…;base64,` URI of at
     * most 32 KB. The hosted page only loads images from itself and data:
     * URIs, so remote URLs are ignored.
     */
    logo?: string;
    fullscreenOnMobile?: boolean;
    mobileBreakpoint?: number;
}
interface PlaidifyErrorResponse {
    detail?: string | {
        msg?: string;
    }[];
    error?: string;
    error_code?: string;
}

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

declare class Plaidify {
    private readonly baseUrl;
    private readonly timeout;
    private token?;
    private apiKey?;
    constructor(config: PlaidifyConfig);
    /** Update the bearer token (e.g. after login). */
    setToken(token: string): void;
    private authHeaders;
    private request;
    private get;
    private post;
    private patch;
    private del;
    private sleep;
    health(): Promise<HealthStatus>;
    listBlueprints(): Promise<BlueprintListResult>;
    getBlueprint(site: string): Promise<BlueprintInfo>;
    connect(site: string, username: string, password: string, options?: {
        extractFields?: string[];
    }): Promise<ConnectResult>;
    submitMfa(sessionId: string, code: string): Promise<MfaSubmitResult>;
    listAccessJobs(options?: {
        limit?: number;
        site?: string;
        status?: string;
        jobType?: string;
    }): Promise<AccessJobListResult>;
    getAccessJob(jobId: string): Promise<AccessJob>;
    waitForAccessJob(jobId: string, options?: {
        pollIntervalMs?: number;
        timeoutMs?: number;
    }): Promise<AccessJob>;
    /**
     * Create an account and use its access token for later calls. A server
     * that has the email address proven first answers with a
     * `RegistrationPending` instead: finish with `verifyEmail()`.
     */
    register(username: string, email: string, password: string): Promise<AuthToken | RegistrationPending>;
    /** Finish a sign-up with the one-time token from the email, and keep the new account's token. */
    verifyEmail(token: string): Promise<AuthToken>;
    /** Log in (OAuth2 password form at POST /auth/token) and keep the token. */
    login(username: string, password: string): Promise<AuthToken>;
    me(): Promise<UserProfile>;
    createLinkSession(site?: string): Promise<LinkSession>;
    createPublicLinkSession(): Promise<LinkSession>;
    createHostedLinkBootstrap(options?: HostedLinkBootstrapRequest): Promise<HostedLinkBootstrapResponse>;
    exchangeHostedLinkBootstrap(launchToken: string): Promise<LinkSession>;
    getLinkUrl(linkToken: string, options?: HostedLinkUrlOptions): string;
    /**
     * Register a webhook for a link session. Each delivery carries
     * `X-Plaidify-Delivery` (the same id on every retry, for de-duplication),
     * `X-Plaidify-Timestamp` (Unix seconds) and `X-Plaidify-Signature`:
     * `sha256=` + hex HMAC-SHA256, keyed with `secret`, of
     * `` `${timestamp}.${rawBody}` ``. Recompute it over the raw body and reject
     * timestamps older than a few minutes.
     */
    registerWebhook(linkToken: string, url: string, secret: string): Promise<WebhookRegistration>;
    exchangePublicToken(publicToken: string): Promise<PublicTokenExchangeResult>;
    listLinks(): Promise<LinkInfo[]>;
    deleteLink(linkToken: string): Promise<{
        status: string;
    }>;
    listTokens(): Promise<AccessTokenInfo[]>;
    deleteToken(accessToken: string): Promise<{
        status: string;
    }>;
    registerAgent(name: string, options?: {
        description?: string;
        allowedScopes?: string[];
        allowedSites?: string[];
        rateLimit?: string;
    }): Promise<AgentInfo>;
    listAgents(): Promise<AgentListResult>;
    getAgent(agentId: string): Promise<AgentInfo>;
    updateAgent(agentId: string, updates: {
        name?: string;
        description?: string;
        allowedScopes?: string[];
        allowedSites?: string[];
        rateLimit?: string;
    }): Promise<{
        status: string;
        agent_id: string;
    }>;
    deactivateAgent(agentId: string): Promise<{
        status: string;
        agent_id: string;
    }>;
    requestConsent(accessToken: string, scopes: string[], agentName: string, durationSeconds?: number, options?: {
        agentDescription?: string;
    }): Promise<ConsentRequest>;
    /** `requestId` is the `request_id` returned by {@link requestConsent}. */
    approveConsent(requestId: string): Promise<ConsentGrant>;
    denyConsent(requestId: string): Promise<{
        request_id: string;
        status: string;
    }>;
    listConsents(): Promise<ConsentListResult>;
    revokeConsent(consentToken: string): Promise<{
        status: string;
        consent_token: string;
    }>;
    /** The raw key is only in this response (`key`) — store it now. */
    createApiKey(name: string, options?: {
        scopes?: string[];
        expiresDays?: number;
    }): Promise<ApiKeyCreated>;
    listApiKeys(): Promise<ApiKeyInfo[]>;
    revokeApiKey(keyId: string): Promise<{
        status: string;
        key_id: string;
    }>;
    listWebhooks(): Promise<WebhookListResult>;
    deleteWebhook(webhookId: string): Promise<{
        status: string;
    }>;
    testWebhook(webhookId: string): Promise<{
        status: string;
    }>;
    getWebhookDeliveries(webhookId: string): Promise<WebhookDeliveryResult>;
    getAuditLogs(options?: {
        eventType?: string;
        offset?: number;
        limit?: number;
    }): Promise<AuditLogResult>;
    verifyAuditChain(): Promise<AuditVerifyResult>;
    scheduleRefresh(accessToken: string, intervalSeconds?: number, options?: {
        scheduleFormat?: string;
    }): Promise<RefreshScheduleResult>;
    unscheduleRefresh(accessToken: string): Promise<{
        status: string;
        access_token: string;
    }>;
    /** The caller's own schedules, tokens masked. */
    listRefreshJobs(): Promise<RefreshJobListResult>;
    /** Tokens go in the body: a query string would land in access logs. */
    fetchData(accessToken: string, consentToken?: string): Promise<ConnectResult>;
}

/**
 * The hosted Link page URL, shared by the client, the React hook and the
 * React Native helpers so they all speak the parameters the page reads
 * (frontend-next/src/config.ts and branding.ts).
 */

declare function buildHostedLinkUrl(serverUrl: string, linkToken: string, options?: HostedLinkUrlOptions, 
/** Resolves a relative `serverUrl` (e.g. `window.location.href`). */
base?: string): string;

/**
 * Error classes for the Plaidify SDK.
 */
declare class PlaidifyError extends Error {
    readonly statusCode?: number;
    readonly detail: string;
    /** Machine-readable code from the server's `{error, error_code}` body, when sent. */
    readonly errorCode?: string;
    constructor(message: string, statusCode?: number, errorCode?: string);
}
declare class AuthenticationError extends PlaidifyError {
    constructor(message?: string, errorCode?: string);
}
declare class NotFoundError extends PlaidifyError {
    constructor(message?: string, errorCode?: string);
}
declare class RateLimitError extends PlaidifyError {
    constructor(message?: string, errorCode?: string);
}
declare class ServerError extends PlaidifyError {
    constructor(message?: string, errorCode?: string);
}

export { type AccessJob, type AccessJobListResult, type AccessTokenInfo, type AgentInfo, type AgentListResult, type ApiKeyCreated, type ApiKeyInfo, type AuditChainError, type AuditEntry, type AuditLogResult, type AuditVerifyResult, type AuthToken, AuthenticationError, type BlueprintInfo, type BlueprintListResult, type BlueprintSummary, type ConnectResult, type ConsentGrant, type ConsentGrantInfo, type ConsentListResult, type ConsentRequest, type HealthStatus, type HostedLinkBootstrapRequest, type HostedLinkBootstrapResponse, type HostedLinkUrlOptions, type LinkEvent, type LinkInfo, type LinkSession, type LinkTheme, type MFAChallenge, type MfaSubmitResult, NotFoundError, Plaidify, type PlaidifyConfig, PlaidifyError, type PlaidifyErrorResponse, type PlaidifyLinkConfig, type PlaidifyLinkEventName, type PlaidifyLinkEventPayload, type PlaidifyLinkExitDetails, type PlaidifyLinkMfaDetails, type PlaidifyLinkSuccessMetadata, type PublicTokenExchangeResult, RateLimitError, type RefreshJobInfo, type RefreshJobListResult, type RefreshScheduleResult, type RegistrationPending, ServerError, type UserProfile, type WebhookDelivery, type WebhookDeliveryResult, type WebhookInfo, type WebhookListResult, type WebhookRegistration, buildHostedLinkUrl };
