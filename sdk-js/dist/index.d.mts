/**
 * Plaidify SDK type definitions.
 */
interface PlaidifyConfig {
    /** Base URL of the Plaidify server (e.g. "http://localhost:8000"). */
    serverUrl: string;
    /** Bearer token for authenticated requests. */
    token?: string;
    /** API key for agent authentication (alternative to bearer token). */
    apiKey?: string;
    /** Request timeout in milliseconds (default: 30000). */
    timeout?: number;
}
interface HealthStatus {
    status: string;
    version: string;
}
interface BlueprintInfo {
    name: string;
    display_name?: string;
    domain?: string;
    category?: string;
    auth_type?: string;
    fields?: string[];
    mfa_type?: string;
}
interface BlueprintListResult {
    blueprints: BlueprintInfo[];
    count: number;
}
interface ConnectResult {
    status: string;
    job_id?: string;
    data?: Record<string, unknown>;
    session_id?: string;
    mfa_type?: string;
    metadata?: Record<string, unknown>;
}
interface AccessJob {
    job_id: string;
    site: string;
    job_type: string;
    status: string;
    session_id?: string;
    mfa_type?: string;
    error_message?: string;
    metadata?: Record<string, unknown>;
    result?: Record<string, unknown>;
    created_at?: string;
    started_at?: string;
    completed_at?: string;
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
interface AuthToken {
    access_token: string;
    refresh_token?: string;
    token_type: string;
}
interface UserProfile {
    id: number;
    email: string;
    created_at?: string;
}
interface LinkSession {
    link_token: string;
    link_url?: string;
    status?: string;
    public_token?: string;
    expiry?: string;
    expires_in?: number;
    public_key?: string;
    scopes?: string[] | null;
}
interface HostedLinkUrlOptions {
    origin?: string;
    theme?: LinkTheme;
}
type PlaidifyLinkEventName = "OPEN" | "CLOSE" | "INSTITUTION_SELECTED" | "CREDENTIALS_SUBMITTED" | "MFA_REQUIRED" | "MFA_SUBMITTED" | "CONNECTED" | "ERROR" | "EXIT" | "DONE";
interface PlaidifyLinkMfaDetails {
    mfa_type?: string;
    session_id?: string;
}
interface PlaidifyLinkExitDetails {
    reason?: string;
    error?: string;
}
interface PlaidifyLinkSuccessMetadata {
    data?: Record<string, unknown>;
    organization_id?: string;
    organization_name?: string;
    public_token?: string;
    site?: string;
}
interface PlaidifyLinkEventPayload extends PlaidifyLinkExitDetails, PlaidifyLinkMfaDetails {
    source?: "plaidify-link";
    event?: PlaidifyLinkEventName | string;
    access_token?: string;
    public_token?: string;
    organization_id?: string;
    organization_name?: string;
    site?: string;
    data?: Record<string, unknown>;
}
interface LinkEvent {
    event: string;
    link_token: string;
    timestamp: string;
    data?: Record<string, unknown>;
}
interface WebhookRegistration {
    webhook_id: string;
    url: string;
    status: string;
}
interface AgentInfo {
    agent_id: string;
    name: string;
    description?: string;
    api_key?: string;
    api_key_prefix?: string;
    allowed_scopes?: string[];
    allowed_sites?: string[];
    rate_limit?: string;
    is_active?: boolean;
    created_at?: string;
}
interface AgentListResult {
    agents: AgentInfo[];
    count: number;
}
interface ConsentRequest {
    consent_request_id: number;
    status: string;
}
interface ConsentGrant {
    consent_token: string;
    scopes: string[];
    expires_at?: string;
}
interface ApiKeyInfo {
    id: string;
    name: string;
    key_prefix: string;
    raw_key?: string;
    scopes?: string;
    is_active?: boolean;
    expires_at?: string;
    last_used_at?: string;
    created_at?: string;
}
interface AuditEntry {
    id: number;
    event_type: string;
    action: string;
    user_id?: number;
    agent_id?: string;
    resource?: string;
    metadata?: Record<string, unknown>;
    ip_address?: string;
    timestamp?: string;
    entry_hash?: string;
}
interface AuditLogResult {
    entries: AuditEntry[];
    total: number;
    offset: number;
    limit: number;
}
interface AuditVerifyResult {
    valid: boolean;
    total: number;
    errors: string[];
}
interface WebhookDelivery {
    id: string;
    status: string;
    status_code?: number;
    attempted_at?: string;
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
    access_token: string;
    interval_seconds: number;
}
interface PlaidifyLinkConfig {
    /** Plaidify server URL. */
    serverUrl: string;
    /** Link token from POST /link/sessions or POST /link/sessions/public. */
    token: string;
    /** Theme overrides for the link UI. */
    theme?: LinkTheme;
    /** Called when link completes successfully. */
    onSuccess?: (accessToken: string, metadata: PlaidifyLinkSuccessMetadata) => void;
    /** Called when the user exits the link flow. */
    onExit?: (details: PlaidifyLinkExitDetails) => void;
    /** Called on each link event. */
    onEvent?: (event: PlaidifyLinkEventName | string, data: PlaidifyLinkEventPayload) => void;
    /** Called when the provider requires additional verification. */
    onMFA?: (details: PlaidifyLinkMfaDetails) => void;
}
interface LinkTheme {
    accentColor?: string;
    bgColor?: string;
    borderRadius?: string;
    logo?: string;
    fullscreenOnMobile?: boolean;
    mobileBreakpoint?: number;
}
interface PlaidifyErrorResponse {
    detail: string;
    status_code?: number;
}

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

declare class Plaidify {
    private readonly baseUrl;
    private readonly timeout;
    private token?;
    private apiKey?;
    constructor(config: PlaidifyConfig);
    /** Update the bearer token (e.g. after login). */
    setToken(token: string): void;
    private headers;
    private request;
    private get;
    private post;
    private patch;
    private del;
    private sleep;
    health(): Promise<HealthStatus>;
    listBlueprints(): Promise<BlueprintListResult>;
    getBlueprint(name: string): Promise<BlueprintInfo>;
    connect(site: string, username: string, password: string, options?: {
        extractFields?: string[];
    }): Promise<ConnectResult>;
    submitMfa(sessionId: string, code: string): Promise<ConnectResult>;
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
    register(email: string, password: string): Promise<AuthToken>;
    login(email: string, password: string): Promise<AuthToken>;
    me(): Promise<UserProfile>;
    createLinkSession(site?: string): Promise<LinkSession>;
    createPublicLinkSession(): Promise<LinkSession>;
    getLinkUrl(linkToken: string, options?: HostedLinkUrlOptions): string;
    registerWebhook(linkToken: string, url: string): Promise<WebhookRegistration>;
    exchangePublicToken(publicToken: string): Promise<PublicTokenExchangeResult>;
    listLinks(): Promise<{
        links: unknown[];
    }>;
    deleteLink(linkToken: string): Promise<{
        detail: string;
    }>;
    listTokens(): Promise<{
        tokens: unknown[];
    }>;
    deleteToken(accessToken: string): Promise<{
        detail: string;
    }>;
    registerAgent(name: string, options?: {
        description?: string;
        allowedScopes?: string[];
        allowedSites?: string[];
        rateLimit?: string;
    }): Promise<AgentInfo>;
    listAgents(): Promise<AgentListResult>;
    getAgent(agentId: string): Promise<AgentInfo>;
    updateAgent(agentId: string, updates: Partial<Pick<AgentInfo, "name" | "description"> & {
        allowedScopes?: string[];
        allowedSites?: string[];
        rateLimit?: string;
    }>): Promise<{
        status: string;
        agent_id: string;
    }>;
    deactivateAgent(agentId: string): Promise<{
        status: string;
        agent_id: string;
    }>;
    requestConsent(accessToken: string, scopes: string[], agentName: string, durationSeconds?: number): Promise<ConsentRequest>;
    approveConsent(consentId: number): Promise<ConsentGrant>;
    denyConsent(consentId: number): Promise<{
        detail: string;
    }>;
    listConsents(): Promise<unknown[]>;
    revokeConsent(consentToken: string): Promise<{
        detail: string;
    }>;
    createApiKey(name: string, options?: {
        scopes?: string;
        expiresDays?: number;
    }): Promise<ApiKeyInfo>;
    listApiKeys(): Promise<{
        keys: ApiKeyInfo[];
    }>;
    revokeApiKey(keyId: string): Promise<{
        detail: string;
    }>;
    listWebhooks(): Promise<{
        webhooks: unknown[];
    }>;
    deleteWebhook(webhookId: string): Promise<{
        detail: string;
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
    scheduleRefresh(accessToken: string, intervalSeconds?: number): Promise<RefreshScheduleResult>;
    unscheduleRefresh(accessToken: string): Promise<{
        status: string;
    }>;
    listRefreshJobs(): Promise<{
        jobs: Record<string, unknown>;
    }>;
    fetchData(accessToken: string, consentToken?: string): Promise<ConnectResult>;
}

/**
 * Error classes for the Plaidify SDK.
 */
declare class PlaidifyError extends Error {
    readonly statusCode?: number;
    readonly detail: string;
    constructor(message: string, statusCode?: number);
}
declare class AuthenticationError extends PlaidifyError {
    constructor(message?: string);
}
declare class NotFoundError extends PlaidifyError {
    constructor(message?: string);
}
declare class RateLimitError extends PlaidifyError {
    constructor(message?: string);
}
declare class ServerError extends PlaidifyError {
    constructor(message?: string);
}

export { type AccessJob, type AccessJobListResult, type AgentInfo, type AgentListResult, type ApiKeyInfo, type AuditEntry, type AuditLogResult, type AuditVerifyResult, type AuthToken, AuthenticationError, type BlueprintInfo, type BlueprintListResult, type ConnectResult, type ConsentGrant, type ConsentRequest, type HealthStatus, type HostedLinkUrlOptions, type LinkEvent, type LinkSession, type LinkTheme, type MFAChallenge, NotFoundError, Plaidify, type PlaidifyConfig, PlaidifyError, type PlaidifyErrorResponse, type PlaidifyLinkConfig, type PlaidifyLinkEventName, type PlaidifyLinkEventPayload, type PlaidifyLinkExitDetails, type PlaidifyLinkMfaDetails, type PlaidifyLinkSuccessMetadata, type PublicTokenExchangeResult, RateLimitError, type RefreshScheduleResult, ServerError, type UserProfile, type WebhookDelivery, type WebhookDeliveryResult, type WebhookRegistration };
