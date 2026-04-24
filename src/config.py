"""
Plaidify configuration management.

All configuration is loaded from environment variables via Pydantic Settings.
No hardcoded secrets — the app will fail fast if required secrets are not set.
"""

from typing import Optional

from pydantic import Field, field_validator
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
    hosted_link_frontend: str = Field(
        default="legacy",
        description="Which hosted /link frontend to serve. 'legacy' serves frontend/link.html; 'react' serves the frontend-next/dist bundle when it is present.",
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
    rate_limit_default: str = Field(
        default="60/minute",
        description="Default rate limit for all other endpoints. Format: 'N/period'.",
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
        description="Milliseconds before a worker may reclaim an unacked access job stream message.",
    )
    access_job_worker_block_ms: int = Field(
        default=5000,
        description="Milliseconds workers block waiting for the next access job.",
    )
    access_job_worker_concurrency: int = Field(
        default=2,
        description="Number of concurrent access job consumers in a worker process.",
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
        description="Optional bearer token for /health/detailed. If unset, detailed health is unrestricted; authenticated access remains valid when the token is configured.",
    )

    # ── Audit Retention ───────────────────────────────────────────
    audit_retention_days: int = Field(
        default=730,
        description="Number of days to retain audit log entries. Older entries are archived/deleted.",
    )

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str, info) -> str:
        env = info.data.get("env", "development")
        if env == "production" and v.startswith("sqlite"):
            raise ValueError(
                "SQLite is not supported in production. Set DATABASE_URL to a PostgreSQL connection string."
            )
        return v

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

    @field_validator("hosted_link_frontend")
    @classmethod
    def validate_hosted_link_frontend(cls, v: str) -> str:
        normalized = (v or "legacy").strip().lower()
        allowed = {"legacy", "react"}
        if normalized not in allowed:
            raise ValueError(
                f"HOSTED_LINK_FRONTEND must be one of {sorted(allowed)}; got {v!r}."
            )
        return normalized

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
