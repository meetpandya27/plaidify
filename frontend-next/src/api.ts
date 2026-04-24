/**
 * Typed API client for the Plaidify hosted-link flow.
 *
 * The legacy vanilla-JS page hit a handful of endpoints through an
 * untyped `apiCall` helper. This module narrows the types and keeps
 * every network call in one place so the React shell in App.tsx reads
 * as pure UI + dispatch.
 */

export interface LinkSessionStatus {
  readonly status: string;
  readonly job_id?: string | null;
  readonly session_id?: string | null;
  readonly site?: string | null;
  readonly public_token?: string | null;
  readonly metadata?: Record<string, unknown> | null;
  readonly mfa_type?: string | null;
  readonly message?: string | null;
  readonly error_message?: string | null;
}

export type OrganizationAuthStyle =
  | "username_password"
  | "email_password"
  | "member_number";

export type SchemaFieldType = "text" | "email" | "password" | "tel" | "number";

export interface SchemaField {
  readonly id: string;
  readonly label: string;
  readonly type: SchemaFieldType;
  readonly autocomplete?: string;
  readonly inputmode?: "text" | "numeric" | "tel" | "email" | "decimal" | "search" | "url" | "none";
  readonly placeholder?: string;
  readonly help_text?: string;
  readonly pattern?: string;
  readonly min_length?: number;
  readonly max_length?: number;
  readonly required?: boolean;
  readonly secret?: boolean;
  readonly reveal?: boolean;
}

export interface CredentialSchema {
  readonly fields: readonly SchemaField[];
  readonly submit_label?: string;
}

export interface MfaSchemaEntry {
  readonly title?: string;
  readonly help_text?: string;
  readonly submit_label?: string;
  readonly fields: readonly SchemaField[];
}

export type MfaSchema = Readonly<Record<string, MfaSchemaEntry>>;

export interface Organization {
  readonly organization_id: string;
  readonly name: string;
  readonly site: string;
  readonly country_code?: string;
  readonly region_code?: string;
  readonly category_label?: string;
  readonly service_area?: string;
  readonly has_mfa?: boolean;
  readonly logo_url?: string;
  readonly logo_monogram?: string;
  readonly primary_color?: string;
  readonly secondary_color?: string;
  readonly accent_color?: string;
  readonly hint_copy?: string;
  readonly auth_style?: OrganizationAuthStyle;
  readonly credential_schema?: CredentialSchema;
  readonly mfa_schema?: MfaSchema;
}

export interface OrganizationSearchResponse {
  readonly results: readonly Organization[];
  readonly count?: number;
  readonly summary?: {
    readonly total_count?: number;
    readonly countries?: readonly { code: string; label: string; count: number }[];
    readonly categories?: readonly { key: string; label: string; count: number }[];
  } | null;
}

export interface EncryptionPublicKeyResponse {
  readonly public_key: string;
}

export interface ConnectResponse {
  readonly status: "connected" | "mfa_required" | "pending" | "error" | string;
  readonly job_id?: string | null;
  readonly session_id?: string | null;
  readonly public_token?: string | null;
  readonly mfa_type?: string | null;
  readonly error?: string | null;
  readonly detail?: string | null;
  readonly metadata?: Record<string, unknown> | null;
}

export interface EncryptedCredentials {
  readonly username: string;
  readonly password: string;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

function joinUrl(base: string, path: string): string {
  return `${base.replace(/\/$/, "")}${path}`;
}

async function request<T>(
  fetchImpl: typeof fetch,
  method: string,
  url: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetchImpl(url, init);
  const text = await response.text();
  const payload = text ? (JSON.parse(text) as unknown) : ({} as unknown);
  if (!response.ok) {
    const record = payload as { detail?: string; error?: string };
    const message =
      record.detail || record.error || response.statusText || "Request failed";
    throw new ApiError(message, response.status);
  }
  return payload as T;
}

export interface LinkApiOptions {
  readonly serverUrl: string;
  readonly linkToken: string;
  readonly fetchImpl?: typeof fetch;
}

export class LinkApi {
  private readonly fetchImpl: typeof fetch;
  private readonly serverUrl: string;
  private readonly linkToken: string;

  constructor(options: LinkApiOptions) {
    if (!options.linkToken) {
      throw new Error("LinkApi requires a linkToken.");
    }
    if (!options.serverUrl) {
      throw new Error("LinkApi requires a serverUrl.");
    }
    this.serverUrl = options.serverUrl.replace(/\/$/, "");
    this.linkToken = options.linkToken;
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  getStatus(): Promise<LinkSessionStatus> {
    return request<LinkSessionStatus>(
      this.fetchImpl,
      "GET",
      joinUrl(
        this.serverUrl,
        `/link/sessions/${encodeURIComponent(this.linkToken)}/status`,
      ),
    );
  }

  searchOrganizations(params: {
    query?: string;
    site?: string;
    limit?: number;
  }): Promise<OrganizationSearchResponse> {
    const search = new URLSearchParams();
    if (params.query) {
      search.set("q", params.query);
    }
    if (params.site) {
      search.set("site", params.site);
    }
    search.set("limit", String(params.limit ?? 40));
    return request<OrganizationSearchResponse>(
      this.fetchImpl,
      "GET",
      joinUrl(this.serverUrl, `/organizations/search?${search.toString()}`),
    );
  }

  getEncryptionPublicKey(): Promise<EncryptionPublicKeyResponse> {
    return request<EncryptionPublicKeyResponse>(
      this.fetchImpl,
      "GET",
      joinUrl(
        this.serverUrl,
        `/encryption/public_key/${encodeURIComponent(this.linkToken)}`,
      ),
    );
  }

  connect(params: {
    site: string;
    encrypted: EncryptedCredentials;
  }): Promise<ConnectResponse> {
    return request<ConnectResponse>(
      this.fetchImpl,
      "POST",
      joinUrl(this.serverUrl, "/connect"),
      {
        link_token: this.linkToken,
        site: params.site,
        encrypted_username: params.encrypted.username,
        encrypted_password: params.encrypted.password,
      },
    );
  }

  submitMfa(params: { sessionId: string; code: string }): Promise<ConnectResponse> {
    const search = new URLSearchParams({
      session_id: params.sessionId,
      code: params.code,
    });
    return request<ConnectResponse>(
      this.fetchImpl,
      "POST",
      joinUrl(this.serverUrl, `/mfa/submit?${search.toString()}`),
    );
  }
}

/**
 * RSA-OAEP credential encryption identical to the legacy hosted-link
 * page. The server returns a SPKI public key in PEM form; we strip the
 * header/footer, base64-decode, import via WebCrypto, and encrypt
 * username + password separately so the backend can store them as
 * independently-wrapped blobs.
 */
export async function encryptCredentials(
  publicKeyPem: string,
  username: string,
  password: string,
  subtle: SubtleCrypto = globalThis.crypto.subtle,
): Promise<EncryptedCredentials> {
  const body = publicKeyPem
    .replace(/-----BEGIN PUBLIC KEY-----/, "")
    .replace(/-----END PUBLIC KEY-----/, "")
    .replace(/\s/g, "");
  const binary = Uint8Array.from(atob(body), (character) => character.charCodeAt(0));
  const cryptoKey = await subtle.importKey(
    "spki",
    binary.buffer,
    { name: "RSA-OAEP", hash: "SHA-256" },
    false,
    ["encrypt"],
  );

  const encoder = new TextEncoder();
  const [encryptedUsername, encryptedPassword] = await Promise.all([
    subtle.encrypt({ name: "RSA-OAEP" }, cryptoKey, encoder.encode(username)),
    subtle.encrypt({ name: "RSA-OAEP" }, cryptoKey, encoder.encode(password)),
  ]);

  return {
    username: bytesToBase64(new Uint8Array(encryptedUsername)),
    password: bytesToBase64(new Uint8Array(encryptedPassword)),
  };
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

/**
 * Poll /link/sessions/{token}/status until the session reaches a
 * terminal state, an MFA prompt, or the timeout. Matches the legacy
 * page's 90-attempt / 1100 ms cadence so existing backends behave the
 * same under the React shell.
 */
export interface PollOptions {
  readonly api: LinkApi;
  readonly maxAttempts?: number;
  readonly intervalMs?: number;
  readonly sleep?: (ms: number) => Promise<void>;
  readonly onTick?: (status: LinkSessionStatus, attempt: number) => void;
}

const DEFAULT_POLL_MAX = 90;
const DEFAULT_POLL_INTERVAL_MS = 1100;

const defaultSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => globalThis.setTimeout(resolve, ms));

export async function pollLinkSession(
  options: PollOptions,
): Promise<LinkSessionStatus> {
  const maxAttempts = options.maxAttempts ?? DEFAULT_POLL_MAX;
  const intervalMs = options.intervalMs ?? DEFAULT_POLL_INTERVAL_MS;
  const sleep = options.sleep ?? defaultSleep;

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const status = await options.api.getStatus();
    options.onTick?.(status, attempt);

    if (
      status.status === "completed" ||
      status.status === "error" ||
      status.status === "mfa_required"
    ) {
      return status;
    }
    await sleep(intervalMs);
  }

  return { status: "timeout" };
}
