"""
Plaidify configuration management.

All configuration is loaded from environment variables via Pydantic Settings.
No hardcoded secrets — the app will fail fast if required secrets are not set.
"""

from typing import Optional

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # ── Database ──────────────────────────────────────────────
    database_url: str = Field(
        default="sqlite:///plaidify.db",
        description="SQLAlchemy database URL. Use PostgreSQL in production.",
    )
    db_pool_size: int = Field(
        default=20,
        description="SQLAlchemy connection pool size. Ignored for SQLite.",
    )
    db_max_overflow: int = Field(
        default=10,
        description="Max overflow connections beyond pool_size. Ignored for SQLite.",
    )
    db_pool_recycle: int = Field(
        default=3600,
        description="Seconds before a connection is recycled. Ignored for SQLite.",
    )

    # ── Encryption ────────────────────────────────────────────
    encryption_key: str = Field(
        ...,  # Required — no default
        description="Base64url-encoded 256-bit key for AES-256-GCM credential encryption. "
        'Generate with: python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"',
    )
    encryption_key_version: int = Field(
        default=1,
        description="Current encryption key version. Increment when rotating keys.",
    )
    encryption_key_previous: Optional[str] = Field(
        default=None,
        description="Previous encryption key (base64url). Set during rotation so old DEKs can still be unwrapped.",
    )

    # ── Key Management Service (KMS) ──────────────────────────
    kms_provider: str = Field(
        default="local",
        description=(
            "KMS backend used to wrap/unwrap data encryption keys. "
            "One of: 'local' (env-var AES-256-GCM, default), "
            "'aws' (AWS KMS), 'azure' (Azure Key Vault), "
            "'vault' (HashiCorp Vault Transit)."
        ),
    )
    kms_key_id: Optional[str] = Field(
        default=None,
        description=(
            "AWS KMS key ARN or alias (the 'aws' provider only; KMS_AWS_KEY_ID is read when unset). "
            "Azure and Vault name their keys with KMS_AZURE_KEY_NAME / KMS_VAULT_KEY_NAME."
        ),
    )
    kms_region: Optional[str] = Field(
        default=None,
        description="Provider region (AWS only). Falls back to AWS_DEFAULT_REGION.",
    )

    # ── JWT / Auth ────────────────────────────────────────────
    jwt_secret_key: str = Field(
        ...,  # Required — no default
        description="Secret key for signing JWT tokens. Generate with: openssl rand -hex 32",
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT signing algorithm.")
    jwt_access_token_expire_minutes: int = Field(
        default=15,  # Short-lived access tokens
        description="JWT access token expiry in minutes.",
    )
    jwt_refresh_token_expire_minutes: int = Field(
        default=60 * 24 * 7,  # 1 week
        description="JWT refresh token expiry in minutes.",
    )

    # ── OAuth2 Social Login ───────────────────────────────────
    oauth_enabled: bool = Field(
        default=False,
        description="Enable POST /auth/oauth2 social login (Google, GitHub).",
    )
    oauth_allowed_providers: str = Field(
        default="google,github",
        description="Comma-separated list of enabled OAuth providers.",
    )
    oauth_google_client_id: Optional[str] = Field(
        default=None,
        description="Google OAuth client id. When set, provider tokens are audience-checked against it.",
    )
    oauth_github_client_id: Optional[str] = Field(
        default=None,
        description=(
            "GitHub OAuth app client id. Required, with OAUTH_GITHUB_CLIENT_SECRET, when github is an "
            "allowed provider: each token is checked with GitHub as issued to this app, so another "
            "app's token is refused."
        ),
    )
    oauth_auto_register: bool = Field(
        default=True,
        description="Auto-create a Plaidify account on first successful OAuth login with a verified email.",
    )

    # ── Registration & Bootstrap ──────────────────────────────
    registration_enabled: bool = Field(
        default=True,
        description=(
            "Allow public self-registration via POST /auth/register. "
            "Recommended to disable in production and provision accounts via the "
            "bootstrap settings below."
        ),
    )
    bootstrap_user_username: Optional[str] = Field(
        default=None,
        description="If set with email + password, create this user on startup (idempotent).",
    )
    bootstrap_user_email: Optional[str] = Field(
        default=None,
        description="Email for the bootstrap user. Required alongside the username/password.",
    )
    bootstrap_user_password: Optional[str] = Field(
        default=None,
        description="Password for the bootstrap user. Provide via secret store / env, not source control.",
    )

    # ── Server ────────────────────────────────────────────────
    app_name: str = Field(default="Plaidify", description="Application name.")
    app_version: str = Field(default="0.3.0b1", description="Application version.")
    env: str = Field(
        default="development",
        description="Environment: 'development', 'staging', or 'production'.",
    )
    debug: bool = Field(default=False, description="Enable debug mode.")
    log_level: str = Field(default="INFO", description="Logging level.")
    log_format: str = Field(default="json", description="Logging format: 'json' or 'text'.")
    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:8000,http://localhost:8080",
        description="Comma-separated list of allowed CORS origins. Must be explicit in production.",
    )
    public_link_sessions_enabled: bool = Field(
        default=False,
        description="Allow anonymous POST /link/sessions/public creation in production. Ignored in development unless explicitly checked.",
    )
    public_link_allowed_origins: str = Field(
        default="",
        description="Comma-separated list of origins allowed to call POST /link/sessions/public. When set, requests from other origins are rejected.",
    )
    link_launch_token_expire_seconds: int = Field(
        default=300,
        description="Seconds before a signed hosted-link bootstrap token expires.",
    )
    enforce_https: bool = Field(
        default=False,
        description="Redirect HTTP to HTTPS and add HSTS header. Auto-enabled in production.",
    )

    # ── Observability ───────────────────────────────────────────
    sentry_dsn: Optional[str] = Field(
        default=None,
        description="Sentry DSN for error tracking. Leave unset to disable.",
    )
    otel_endpoint: Optional[str] = Field(
        default=None,
        description="OpenTelemetry OTLP endpoint (e.g. http://localhost:4317). Leave unset to disable.",
    )

    # ── Connectors ────────────────────────────────────────────
    connectors_dir: str = Field(
        default="connectors",
        description="Path to the directory containing connector blueprints.",
    )

    # ── Demo / Sandbox ────────────────────────────────────────
    demo_mode: bool = Field(
        default=False,
        description=(
            "Enable the bundled sandbox: surfaces connectors tagged 'sandbox'/'demo' "
            "on discovery surfaces (/blueprints, picker catalog) so the full "
            "connect → MFA → extract loop can be exercised end-to-end without a "
            "real site. Keep disabled in production."
        ),
    )
    demo_portal_url: str = Field(
        default="http://127.0.0.1:8799",
        description="Base URL of the bundled demo portal target site used by sandbox connectors.",
    )

    # ── Rate Limiting ─────────────────────────────────────────
    rate_limit_enabled: bool = Field(
        default=True,
        description="Enable rate limiting on API endpoints.",
    )
    rate_limit_auth: str = Field(
        default="5/minute",
        description="Rate limit for auth endpoints (login, register). Format: 'N/period'.",
    )
    rate_limit_connect: str = Field(
        default="10/minute",
        description="Rate limit for /connect endpoint. Format: 'N/period'.",
    )
    rate_limit_mfa: str = Field(
        default="5/minute",
        description="Rate limit for POST /mfa/submit to deter MFA code brute-force. Format: 'N/period'.",
    )
    rate_limit_default: str = Field(
        default="60/minute",
        description=(
            "Limit for every route without its own, per client IP and path. Health probes, /metrics, "
            "the hosted-link page and its assets, its event stream and status polls are exempt. "
            "Format: 'N/period'."
        ),
    )

    # ── Resilience ────────────────────────────────────────────
    llm_circuit_failure_threshold: int = Field(
        default=5,
        description="Consecutive LLM failures before the LLM circuit breaker opens (fail-fast).",
    )
    llm_circuit_reset_seconds: float = Field(
        default=30.0,
        description="Seconds the LLM circuit stays open before allowing a trial call.",
    )
    llm_retry_max_attempts: int = Field(
        default=2,
        description="Extra retry attempts (exponential backoff) for transient LLM rate-limit errors.",
    )
    browser_circuit_failure_threshold: int = Field(
        default=5,
        description="Consecutive browser-launch failures before the browser circuit breaker opens.",
    )
    browser_circuit_reset_seconds: float = Field(
        default=30.0,
        description="Seconds the browser circuit stays open before allowing a trial launch.",
    )

    # ── Redis ─────────────────────────────────────────────────
    redis_url: Optional[str] = Field(
        default=None,
        description="Redis URL for shared state (RSA keys, rate limiting). Example: redis://localhost:6379/0",
    )

    # ── Access Job Execution ─────────────────────────────────
    access_job_execution_mode: str = Field(
        default="inprocess",
        description="How detached access jobs run: 'inprocess' or 'redis-worker'.",
    )
    access_job_stream_key: str = Field(
        default="plaidify:access_jobs:stream",
        description="Redis stream used to dispatch detached access jobs.",
    )
    access_job_consumer_group: str = Field(
        default="plaidify-access-jobs",
        description="Redis consumer group name for access job workers.",
    )
    access_job_payload_ttl: int = Field(
        default=3600,
        description="Seconds to retain encrypted access job dispatch payloads in Redis.",
    )
    access_job_reclaim_idle_ms: int = Field(
        default=30000,
        description=(
            "Milliseconds an access-job stream message may go without a heartbeat before another worker "
            "may claim it. A running job renews its claim every ACCESS_JOB_HEARTBEAT_SECONDS, so only the "
            "messages of a dead worker are reclaimed; a reclaimed job runs only if it never started."
        ),
    )
    access_job_worker_block_ms: int = Field(
        default=5000,
        description="Milliseconds workers block waiting for the next access job.",
    )
    access_job_worker_concurrency: int = Field(
        default=2,
        description="Number of concurrent access job consumers in a worker process.",
    )

    # ── Jobs ──────────────────────────────────────────────────
    # Access-job liveness, the background services (scheduled refresh,
    # webhook outbox, stuck-job reaper) and webhook delivery.
    access_job_heartbeat_seconds: float = Field(
        default=10.0,
        gt=0,
        description=(
            "How often a running access job renews its claim: its scope lock, its stream message "
            "(redis-worker mode) and its heartbeat row. Keep it well below ACCESS_JOB_RECLAIM_IDLE_MS "
            "and ACCESS_JOB_STALE_AFTER_SECONDS."
        ),
    )
    access_job_stale_after_seconds: int = Field(
        default=90,
        ge=10,
        description=(
            "A running access job whose heartbeat is older than this is orphaned (the process running it "
            "died): the reaper fails it, frees its lock and ends its hosted-link session."
        ),
    )
    access_job_queue_timeout_seconds: int = Field(
        default=600,
        ge=10,
        description="An access job still waiting for an executor this long after it was queued is failed.",
    )
    access_job_deadline_margin_seconds: int = Field(
        default=120,
        ge=0,
        description=(
            "Slack added to ENGINE_TIMEOUT_SECONDS + MFA_TIMEOUT_SECONDS to form an access job's hard "
            "deadline, after which it is cancelled and failed."
        ),
    )
    access_job_drain_seconds: float = Field(
        default=25.0,
        ge=0,
        description=(
            "On SIGTERM the executor stops taking jobs and gives running ones this long to finish before "
            "cancelling them. Keep it below the orchestrator's stop grace period."
        ),
    )
    access_job_reaper_interval_seconds: float = Field(
        default=30.0,
        gt=0,
        description="How often the reaper looks for access jobs past their deadline or with a stale heartbeat.",
    )
    background_services_enabled: bool = Field(
        default=True,
        description=(
            "Let this process run the background services (scheduled refresh, webhook outbox, stuck-job "
            "reaper). Each runs in one process at a time, under a lease. With "
            "ACCESS_JOB_EXECUTION_MODE=redis-worker they run in the executor process, not the web workers."
        ),
    )
    redis_socket_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description="Socket timeout for the link-session store's and the access-job dispatcher's Redis calls.",
    )
    link_event_keepalive_seconds: float = Field(
        default=15.0,
        gt=0,
        description="Keep-alive interval of the GET /link/events/{token} server-sent event stream.",
    )
    refresh_tick_seconds: float = Field(
        default=30.0,
        gt=0,
        description="How often the refresh scheduler looks for due scheduled refreshes.",
    )
    refresh_max_concurrency: int = Field(
        default=5,
        ge=1,
        description="Scheduled refreshes that may run at the same time.",
    )
    webhook_max_attempts: int = Field(
        default=10,
        ge=1,
        description="Delivery attempts for one webhook event before it is marked failed.",
    )
    webhook_retry_base_seconds: float = Field(
        default=15.0,
        gt=0,
        description="Delay before a webhook's first retry; it doubles on each attempt up to WEBHOOK_RETRY_MAX_SECONDS.",
    )
    webhook_retry_max_seconds: float = Field(
        default=3600.0,
        gt=0,
        description="Longest delay between two delivery attempts of a webhook event.",
    )
    webhook_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description="Timeout of one webhook delivery request.",
    )
    webhook_poll_interval_seconds: float = Field(
        default=5.0,
        gt=0,
        description="How often the webhook outbox looks for deliveries that are due.",
    )
    webhook_delivery_retention_days: int = Field(
        default=7,
        ge=1,
        description="Days delivered and failed webhook deliveries stay listed before the outbox removes them.",
    )
    webhook_allow_private_targets: bool = Field(
        default=False,
        description=(
            "Deliver webhooks to private-network addresses (10/8, 172.16/12, 192.168/16, fc00::/7). For "
            "development networks only; ignored in production. Loopback is allowed outside production."
        ),
    )

    # ── Browser Engine ────────────────────────────────────────
    browser_headless: bool = Field(
        default=True,
        description="Run Playwright browsers in headless mode.",
    )
    browser_pool_size: int = Field(
        default=5,
        description="Maximum number of concurrent browser contexts in the pool.",
    )
    browser_idle_timeout: int = Field(
        default=300,
        description="Seconds before an idle browser context is closed.",
    )
    browser_navigation_timeout: int = Field(
        default=30000,
        description="Default navigation timeout in milliseconds.",
    )
    browser_action_timeout: int = Field(
        default=10000,
        description="Default timeout for individual actions (click, fill) in milliseconds.",
    )
    browser_block_resources: bool = Field(
        default=True,
        description="Block images, fonts, and analytics scripts for speed.",
    )
    browser_stealth: bool = Field(
        default=True,
        description="Enable anti-detection measures (randomized viewport, user-agent).",
    )
    strict_read_only_mode: bool = Field(
        default=True,
        description="Enforce strict post-auth read-only restrictions for all browser-driven blueprints.",
    )
    browser_allow_read_downloads: bool = Field(
        default=True,
        description="Allow downloads during the post-auth read phase and capture them as temporary browser artifacts.",
    )
    browser_download_root: str = Field(
        default="/tmp/plaidify-downloads",
        description="Root directory for temporary browser download artifacts.",
    )

    # ── LLM Extraction ────────────────────────────────────────
    llm_provider: str = Field(
        default="openai",
        description="LLM provider for adaptive extraction: 'openai' or 'anthropic'.",
    )
    llm_api_key: Optional[str] = Field(
        default=None,
        description="API key for the LLM provider. Required when using LLM extraction.",
    )
    llm_model: Optional[str] = Field(
        default=None,
        description="Model name override (e.g. 'gpt-4o', 'claude-sonnet-4-20250514'). Uses provider default if not set.",
    )
    llm_base_url: Optional[str] = Field(
        default=None,
        description="Override LLM API base URL (for Azure OpenAI, local servers, etc.).",
    )
    llm_max_tokens: int = Field(
        default=4096,
        description="Max completion tokens for LLM extraction responses.",
    )
    llm_temperature: float = Field(
        default=0.0,
        description="LLM temperature (0.0 = deterministic, recommended for extraction).",
    )
    llm_timeout: float = Field(
        default=60.0,
        description="HTTP timeout in seconds for LLM API calls.",
    )
    llm_token_budget: int = Field(
        default=30000,
        description="Max input tokens for DOM sent to LLM. Larger pages are truncated.",
    )
    llm_fallback_model: Optional[str] = Field(
        default=None,
        description="Fallback model if primary fails (e.g. 'gpt-4o' when primary is 'gpt-4o-mini').",
    )

    # ── Engine ────────────────────────────────────────────────
    engine_timeout_seconds: int = Field(
        default=300,
        gt=0,
        description=(
            "Automation time budget for one connection (navigation, login, extraction, logout). "
            "Time spent waiting for the user to answer an MFA challenge is not counted; that has "
            "its own budget, MFA_TIMEOUT_SECONDS."
        ),
    )
    mfa_timeout_seconds: int = Field(
        default=300,
        gt=0,
        description="How long a connection waits for the user to answer an MFA challenge before it ends as mfa_timeout.",
    )
    mfa_max_attempts: int = Field(
        default=3,
        ge=1,
        description="MFA codes the user may submit for one challenge before a rejected code fails the connection.",
    )
    engine_allow_internal_connectors: bool = Field(
        default=False,
        description=(
            "Run connectors tagged internal/fixture/sandbox/demo, and let the browser reach loopback "
            "addresses, outside DEMO_MODE. For tests and local development only."
        ),
    )
    engine_trusted_connectors: str = Field(
        default="",
        description=(
            "Comma-separated site keys in CONNECTORS_DIR that the operator vouches for. Connectors bundled "
            "with Plaidify are trusted automatically; every other blueprint (generated, registry, "
            "user-supplied) is untrusted: it may not run JavaScript and never reaches private networks."
        ),
    )
    engine_redis_socket_timeout: float = Field(
        default=2.0,
        gt=0,
        description="Socket timeout in seconds for the engine's Redis calls (MFA sessions, site rate limits).",
    )
    browser_chromium_sandbox: bool = Field(
        default=True,
        description=(
            "Launch Chromium with its OS sandbox. Keep enabled: the browser renders third-party pages in "
            "a process that also holds the service's secrets. Disable only where the sandbox cannot run."
        ),
    )
    browser_block_private_networks: bool = Field(
        default=True,
        description=(
            "Refuse browser requests to private, loopback, link-local, CGNAT and cloud-metadata addresses "
            "for trusted connectors too (untrusted ones are always refused). Loopback is allowed in "
            "DEMO_MODE or with ENGINE_ALLOW_INTERNAL_CONNECTORS."
        ),
    )
    browser_max_download_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=0,
        description="Largest read-phase download returned inline (base64) with a connection result; larger files are reported but omitted.",
    )
    llm_effort: Optional[str] = Field(
        default="low",
        description=(
            "Reasoning effort requested from models that support it (Anthropic output_config.effort, "
            "OpenAI reasoning_effort): 'low', 'medium', 'high', 'xhigh' or 'max'. Empty uses the model default."
        ),
    )
    llm_server_side_fallbacks: bool = Field(
        default=True,
        description=(
            "On Anthropic's first-party API, ask models whose safety classifiers can decline a request to "
            "re-run a declined request on Anthropic's recommended fallback model (fallbacks: 'default')."
        ),
    )

    @field_validator("llm_effort")
    @classmethod
    def validate_llm_effort(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip().lower()
        if v not in ("low", "medium", "high", "xhigh", "max"):
            raise ValueError("llm_effort must be one of 'low', 'medium', 'high', 'xhigh', 'max'")
        return v

    @field_validator("llm_provider")
    @classmethod
    def validate_llm_provider(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("openai", "anthropic"):
            raise ValueError("llm_provider must be 'openai' or 'anthropic'")
        return v

    @field_validator("env")
    @classmethod
    def validate_env(cls, v: str) -> str:
        v = v.lower()
        if v not in ("development", "staging", "production"):
            raise ValueError("env must be 'development', 'staging', or 'production'")
        return v

    @field_validator("access_job_execution_mode")
    @classmethod
    def validate_access_job_execution_mode(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("inprocess", "redis-worker"):
            raise ValueError("access_job_execution_mode must be 'inprocess' or 'redis-worker'")
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return v

    @field_validator("log_format")
    @classmethod
    def validate_log_format(cls, v: str) -> str:
        v = v.lower()
        if v not in ("json", "text"):
            raise ValueError("log_format must be 'json' or 'text'")
        return v

    # ── Health Check ─────────────────────────────────────────────
    health_check_token: Optional[str] = Field(
        default=None,
        description=(
            "Bearer token for GET /health/detailed; a login or API key also works once it is set. "
            "In production the endpoint is off (404) until it is set; in development, unset leaves it open."
        ),
    )

    # ── Audit Retention ───────────────────────────────────────────
    audit_retention_days: int = Field(
        default=730,
        description="Number of days to retain audit log entries. Older entries are archived/deleted.",
    )

    # ── Data ──────────────────────────────────────────────────────
    audit_hmac_key: Optional[str] = Field(
        default=None,
        min_length=32,
        description=(
            "Secret (at least 32 characters) that signs the audit hash chain with HMAC-SHA256. Keep it "
            "outside the database, e.g. in the secrets manager. If unset, a key is derived from "
            "ENCRYPTION_KEY (HKDF), and rotating ENCRYPTION_KEY then re-seals the chain. "
            'Generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        ),
    )
    audit_hmac_key_previous: Optional[str] = Field(
        default=None,
        min_length=32,
        description="Previous AUDIT_HMAC_KEY, kept while rotating it so older audit entries still verify.",
    )
    result_retention_days: int = Field(
        default=30,
        ge=1,
        description="Days an access job's stored (encrypted) result is kept before the cleanup job erases it.",
    )

    @field_validator("audit_hmac_key", "audit_hmac_key_previous", mode="before")
    @classmethod
    def empty_audit_key_is_unset(cls, v):
        return v or None

    # ── Ops ───────────────────────────────────────────────────────
    access_worker_metrics_port: int = Field(
        default=9101,
        description=(
            "Port on which the access-job executor (python -m src.access_job_worker) serves "
            "/metrics and its /health liveness check. 0 disables. The web app serves /metrics itself."
        ),
    )

    # ── API ───────────────────────────────────────────────────────
    link_launch_secret: Optional[str] = Field(
        default=None,
        description=(
            "Key (at least 32 characters) that signs hosted-link launch tokens. Kept apart from "
            "JWT_SECRET_KEY so a launch token can never pass as a login. If unset, a key is derived "
            "from JWT_SECRET_KEY (HKDF, fixed label)."
        ),
    )
    docs_enabled: bool = Field(
        default=False,
        description="Serve /docs, /redoc and /openapi.json in production. They are always served outside production.",
    )
    metrics_token: Optional[str] = Field(
        default=None,
        description="When set, GET /metrics requires 'Authorization: Bearer <METRICS_TOKEN>'.",
    )
    oauth_github_client_secret: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("oauth_github_client_secret", "github_client_secret"),
        description=(
            "Client secret of the GitHub OAuth app (OAUTH_GITHUB_CLIENT_SECRET or GITHUB_CLIENT_SECRET). "
            "GitHub tokens are checked against that app before they are trusted."
        ),
    )
    rate_limit_encryption: str = Field(
        default="10/minute",
        description=(
            "Per-client limit for the unauthenticated endpoints that generate an RSA key "
            "(POST /encryption/session, GET /encryption/public_key/{token}). Format: 'N/period'."
        ),
    )
    smtp_host: Optional[str] = Field(
        default=None,
        description="SMTP server for password-reset mail. Unset: reset emails are not sent (logged at WARNING).",
    )
    smtp_port: int = Field(default=587, ge=1, le=65535, description="SMTP port (587 for STARTTLS).")
    smtp_username: Optional[str] = Field(default=None, description="SMTP login user; unset for no login.")
    smtp_password: Optional[str] = Field(default=None, description="SMTP login password.")
    smtp_from: Optional[str] = Field(
        default=None,
        description="From address of password-reset mail. Required together with SMTP_HOST.",
    )
    smtp_starttls: bool = Field(
        default=True,
        description="Upgrade the SMTP connection with STARTTLS before logging in or sending. Disable only for a local relay.",
    )
    smtp_timeout_seconds: float = Field(default=10.0, gt=0, description="Socket timeout for SMTP delivery.")
    password_reset_url: Optional[str] = Field(
        default=None,
        description=(
            "Your app's password-reset page, e.g. 'https://app.example.com/reset?token={token}'. When set, "
            "the reset email links to it; otherwise the email carries the one-time code for POST /auth/reset-password."
        ),
    )

    @field_validator(
        "link_launch_secret",
        "metrics_token",
        "oauth_github_client_secret",
        "smtp_host",
        "smtp_username",
        "smtp_password",
        "smtp_from",
        "password_reset_url",
        mode="before",
    )
    @classmethod
    def empty_api_setting_is_unset(cls, v):
        return v or None

    @model_validator(mode="after")
    def _apply_production_rules(self) -> "Settings":
        # Runs once every field is parsed. A field validator on database_url
        # would run before ``env`` is parsed (field order), see the default
        # "development" and never refuse SQLite.
        if self.env == "production":
            if self.database_url.strip().lower().startswith("sqlite"):
                raise ValueError(
                    "SQLite is not supported in production. Set DATABASE_URL to a PostgreSQL connection string."
                )
            # Self-registration is opt-in in production: unless
            # REGISTRATION_ENABLED is set explicitly, accounts come from the
            # BOOTSTRAP_USER_* settings or an administrator.
            if "registration_enabled" not in self.model_fields_set:
                self.registration_enabled = False
        return self

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, v: str, info) -> str:
        env = info.data.get("env", "development")
        origins = [o.strip() for o in v.split(",") if o.strip()]
        if env == "production" and "*" in origins:
            raise ValueError(
                "CORS wildcard (*) is not allowed in production. "
                "Set CORS_ORIGINS to specific origins (e.g. 'https://app.example.com')."
            )
        return v

    model_config = {
        "env_prefix": "",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }


def get_settings() -> Settings:
    """
    Load and return application settings.

    Raises a clear error if required environment variables are missing.
    """
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as e:
        import sys

        print(
            "\n╔══════════════════════════════════════════════════════════════╗",
            file=sys.stderr,
        )
        print(
            "║  PLAIDIFY CONFIGURATION ERROR                                ║",
            file=sys.stderr,
        )
        print(
            "╠══════════════════════════════════════════════════════════════╣",
            file=sys.stderr,
        )
        print(
            "║  Required environment variables are missing.                 ║",
            file=sys.stderr,
        )
        print(
            "║                                                              ║",
            file=sys.stderr,
        )
        print(
            "║  Set the following before starting Plaidify:                 ║",
            file=sys.stderr,
        )
        print(
            "║                                                              ║",
            file=sys.stderr,
        )
        print(
            '║  export ENCRYPTION_KEY="$(python -c                         ║',
            file=sys.stderr,
        )
        print(
            '║    "import base64,os;                                       ║',
            file=sys.stderr,
        )
        print(
            '║    print(base64.urlsafe_b64encode(os.urandom(32)).decode())" ║',
            file=sys.stderr,
        )
        print(
            '║  export JWT_SECRET_KEY="$(openssl rand -hex 32)"            ║',
            file=sys.stderr,
        )
        print(
            "║                                                              ║",
            file=sys.stderr,
        )
        print(
            "║  Or create a .env file (see .env.example).                   ║",
            file=sys.stderr,
        )
        print(
            "╚══════════════════════════════════════════════════════════════╝",
            file=sys.stderr,
        )
        print(f"\nDetails: {e}", file=sys.stderr)
        sys.exit(1)
