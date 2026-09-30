/**
 * Plaidify SDK type definitions.
 *
 * Response types mirror what the server actually returns (src/routers/*.py
 * and src/models.py); fields the server does not send are not declared.
 */

// ── Configuration ────────────────────────────────────────────────────────────

export interface PlaidifyConfig {
  /** Base URL of the Plaidify server (e.g. "http://localhost:8000"). */
  serverUrl: string;
  /** User access token (JWT), sent as `Authorization: Bearer`. */
  token?: string;
  /** API key (`pk_…` / `pk_agent_…`), sent as `X-API-Key`. */
  apiKey?: string;
  /** Request timeout in milliseconds (default: 30000). */
  timeout?: number;
}

// ── Core Models ──────────────────────────────────────────────────────────────

/** GET /health */
export interface HealthStatus {
  status: string;
}

/** An entry of GET /blueprints. */
export interface BlueprintSummary {
  site: string;
  name: string;
  domain: string;
  tags: string[];
  has_mfa: boolean;
  schema_version: string;
}

/** GET /blueprints/{site} */
export interface BlueprintInfo {
  name: string;
  domain: string;
  tags: string[];
  has_mfa: boolean;
  extract_fields: string[];
  schema_version: string;
  rate_limit?: Record<string, unknown> | null;
}

export interface BlueprintListResult {
  blueprints: BlueprintSummary[];
  count: number;
}

/** POST /connect, GET/POST /fetch_data */
export interface ConnectResult {
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
export interface MfaSubmitResult {
  status: string;
  message?: string;
  error?: string;
}

export interface AccessJob {
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

export interface AccessJobListResult {
  jobs: AccessJob[];
  count: number;
}

export interface MFAChallenge {
  session_id: string;
  mfa_type: string;
  prompt?: string;
}

/** POST /auth/register, POST /auth/verify-email, POST /auth/token */
export interface AuthToken {
  access_token: string;
  refresh_token?: string | null;
  token_type: string;
}

/**
 * POST /auth/register (202) while the server has sign-ups prove their email
 * address first. The same whether or not the username or address was free;
 * finish with `verifyEmail(token)`, the token coming from the email.
 */
export interface RegistrationPending {
  status: "verification_sent";
  detail: string;
}

/** GET /auth/me */
export interface UserProfile {
  id: number;
  username: string | null;
  email: string | null;
  is_active: boolean;
}

// ── Link Flow ────────────────────────────────────────────────────────────────

/** POST /link/sessions, /link/sessions/public, /link/sessions/bootstrap */
export interface LinkSession {
  link_token: string;
  link_url: string;
  public_key?: string;
  expires_in: number;
  scopes?: string[] | null;
}

/** An entry of GET /links. */
export interface LinkInfo {
  link_token: string;
  site: string;
}

/** An entry of GET /tokens. */
export interface AccessTokenInfo {
  token: string;
  link_token: string;
}

export interface HostedLinkUrlOptions {
  /** Origin of the page embedding Link (web embeds only). */
  origin?: string;
  theme?: LinkTheme;
}

export interface HostedLinkBootstrapRequest {
  site?: string;
  allowedOrigin?: string;
  /** Every origin allowed to embed the session (up to 20). */
  allowedOrigins?: string[];
  scopes?: string[];
}

export interface HostedLinkBootstrapResponse {
  launch_token: string;
  expires_in: number;
  site?: string | null;
  allowed_origin?: string | null;
  allowed_origins?: string[] | null;
  scopes?: string[] | null;
}

export type PlaidifyLinkEventName =
  | "OPEN"
  | "CLOSE"
  | "INSTITUTION_SELECTED"
  | "CREDENTIALS_SUBMITTED"
  | "MFA_REQUIRED"
  | "MFA_SUBMITTED"
  | "CONNECTED"
  | "ERROR"
  | "EXIT"
  | "DONE"
  | "TELEMETRY"
  | "SUPPORT_REQUESTED";

export interface PlaidifyLinkMfaDetails {
  mfa_type?: string;
  session_id?: string;
}

export interface PlaidifyLinkExitDetails {
  reason?: string;
  error?: string;
  /** Error-taxonomy code of the last error, when the user exits from one. */
  error_code?: string;
}

export interface PlaidifyLinkSuccessMetadata {
  job_id?: string;
  organization_id?: string;
  organization_name?: string;
  public_token?: string;
  site?: string;
}

export interface PlaidifyLinkEventPayload extends PlaidifyLinkExitDetails, PlaidifyLinkMfaDetails {
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
export interface LinkEvent {
  event: string;
  timestamp: string;
  data?: Record<string, unknown>;
}

/** POST /webhooks/register */
export interface WebhookRegistration {
  webhook_id: string;
  status: string;
}

/** An entry of GET /webhooks. */
export interface WebhookInfo {
  webhook_id: string;
  link_token: string;
  url: string;
  created_at: string | null;
}

export interface WebhookListResult {
  webhooks: WebhookInfo[];
  count: number;
}

// ── Agents ───────────────────────────────────────────────────────────────────

export interface AgentInfo {
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

export interface AgentListResult {
  agents: AgentInfo[];
  count: number;
}

// ── Consent ──────────────────────────────────────────────────────────────────

/** POST /consent/request */
export interface ConsentRequest {
  request_id: string;
  agent_name: string;
  scopes: string[];
  duration_seconds: number;
  status: string;
}

/** POST /consent/{request_id}/approve */
export interface ConsentGrant {
  consent_token: string;
  scopes: string[];
  expires_at: string;
  status: string;
}

/** An entry of GET /consent. */
export interface ConsentGrantInfo {
  consent_token: string;
  agent_name: string;
  scopes: string[];
  access_token: string;
  expires_at: string;
  created_at: string | null;
}

export interface ConsentListResult {
  grants: ConsentGrantInfo[];
  count: number;
}

// ── API Keys ─────────────────────────────────────────────────────────────────

/** POST /api-keys — the only response that carries the raw key. */
export interface ApiKeyCreated {
  id: string;
  name: string;
  key: string;
  key_prefix: string;
  expires_at: string | null;
  created_at: string | null;
}

/** An entry of GET /api-keys. */
export interface ApiKeyInfo {
  id: string;
  name: string;
  key_prefix: string;
  /** Scopes the key is limited to; null means every scope. */
  scopes: string[] | null;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string | null;
}

// ── Audit ────────────────────────────────────────────────────────────────────

export interface AuditEntry {
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

export interface AuditLogResult {
  entries: AuditEntry[];
  total: number;
  offset: number;
  limit: number;
}

export interface AuditChainError {
  id: number;
  error: string;
  expected?: string | null;
  actual?: string | null;
}

export interface AuditVerifyResult {
  valid: boolean;
  total: number;
  errors: AuditChainError[];
}

// ── Webhooks ─────────────────────────────────────────────────────────────────

/** One delivery attempt recorded for a webhook. */
export interface WebhookDelivery {
  attempt: number;
  success: boolean;
  timestamp: number;
  status_code?: number;
  error?: string;
}

export interface WebhookDeliveryResult {
  webhook_id: string;
  url: string;
  deliveries: WebhookDelivery[];
  total: number;
}

// ── Public Token ─────────────────────────────────────────────────────────────

export interface PublicTokenExchangeResult {
  access_token: string;
}

// ── Scheduled Refresh ────────────────────────────────────────────────────────

export interface RefreshScheduleResult {
  status: string;
  /** Truncated for display, e.g. "3f2a9c1d-8e4b...". */
  access_token: string;
  interval_seconds: number;
  schedule_format: string;
}

export interface RefreshJobInfo {
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

export interface RefreshJobListResult {
  jobs: RefreshJobInfo[];
}

// ── Link Widget ──────────────────────────────────────────────────────────────

export interface PlaidifyLinkConfig {
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

export interface LinkTheme {
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

// ── Error ────────────────────────────────────────────────────────────────────

export interface PlaidifyErrorResponse {
  detail?: string | { msg?: string }[];
  error?: string;
  error_code?: string;
}
