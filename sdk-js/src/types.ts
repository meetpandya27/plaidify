/**
 * Plaidify SDK type definitions.
 */

// ── Configuration ────────────────────────────────────────────────────────────

export interface PlaidifyConfig {
  /** Base URL of the Plaidify server (e.g. "http://localhost:8000"). */
  serverUrl: string;
  /** Bearer token for authenticated requests. */
  token?: string;
  /** API key for agent authentication (alternative to bearer token). */
  apiKey?: string;
  /** Request timeout in milliseconds (default: 30000). */
  timeout?: number;
}

// ── Core Models ──────────────────────────────────────────────────────────────

export interface HealthStatus {
  status: string;
  version: string;
}

export interface BlueprintInfo {
  name: string;
  display_name?: string;
  domain?: string;
  category?: string;
  auth_type?: string;
  fields?: string[];
  mfa_type?: string;
}

export interface BlueprintListResult {
  blueprints: BlueprintInfo[];
  count: number;
}

export interface ConnectResult {
  status: string;
  data?: Record<string, unknown>;
  session_id?: string;
  mfa_type?: string;
}

export interface MFAChallenge {
  session_id: string;
  mfa_type: string;
  prompt?: string;
}

export interface AuthToken {
  access_token: string;
  refresh_token?: string;
  token_type: string;
}

export interface UserProfile {
  id: number;
  email: string;
  created_at?: string;
}

// ── Link Flow ────────────────────────────────────────────────────────────────

export interface LinkSession {
  link_token: string;
  status: string;
  public_token?: string;
  expiry?: string;
  public_key?: string;
}

export interface LinkEvent {
  event: string;
  link_token: string;
  timestamp: string;
  data?: Record<string, unknown>;
}

export interface WebhookRegistration {
  webhook_id: string;
  url: string;
  status: string;
}

// ── Agents ───────────────────────────────────────────────────────────────────

export interface AgentInfo {
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

export interface AgentListResult {
  agents: AgentInfo[];
  count: number;
}

// ── Consent ──────────────────────────────────────────────────────────────────

export interface ConsentRequest {
  consent_request_id: number;
  status: string;
}

export interface ConsentGrant {
  consent_token: string;
  scopes: string[];
  expires_at?: string;
}

// ── API Keys ─────────────────────────────────────────────────────────────────

export interface ApiKeyInfo {
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

// ── Audit ────────────────────────────────────────────────────────────────────

export interface AuditEntry {
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

export interface AuditLogResult {
  entries: AuditEntry[];
  total: number;
  offset: number;
  limit: number;
}

export interface AuditVerifyResult {
  valid: boolean;
  total: number;
  errors: string[];
}

// ── Webhooks ─────────────────────────────────────────────────────────────────

export interface WebhookDelivery {
  id: string;
  status: string;
  status_code?: number;
  attempted_at?: string;
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
  access_token: string;
  interval_seconds: number;
}

// ── Link Widget ──────────────────────────────────────────────────────────────

export interface PlaidifyLinkConfig {
  /** Plaidify server URL. */
  serverUrl: string;
  /** Link token from POST /link/create. */
  token: string;
  /** Theme overrides for the link UI. */
  theme?: LinkTheme;
  /** Called when link completes successfully. */
  onSuccess?: (publicToken: string, metadata: Record<string, unknown>) => void;
  /** Called when the user exits the link flow. */
  onExit?: (error?: string) => void;
  /** Called on each link event. */
  onEvent?: (event: string, data: Record<string, unknown>) => void;
}

export interface LinkTheme {
  accentColor?: string;
  bgColor?: string;
  borderRadius?: string;
  logo?: string;
}

// ── Error ────────────────────────────────────────────────────────────────────

export interface PlaidifyErrorResponse {
  detail: string;
  status_code?: number;
}
