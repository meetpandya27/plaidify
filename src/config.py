"""
Plaidify configuration management.

All configuration is loaded from environment variables via Pydantic Settings.
No hardcoded secrets — the app will fail fast if required secrets are not set.
"""

from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from typing import Optional


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
        "Generate with: python -c \"import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())\"",
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
        description="Secret key for signing JWT tokens. "
        "Generate with: openssl rand -hex 32",
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
    app_version: str = Field(default="0.3.0a1", description="Application version.")
    env: str = Field(
        default="development",
        description="Environment: 'development', 'staging', or 'production'.",
    )
    debug: bool = Field(default=False, description="Enable debug mode.")
    log_level: str = Field(default="INFO", description="Logging level.")
    log_format: str = Field(
        default="json", description="Logging format: 'json' or 'text'."
    )
    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:8000,http://localhost:8080",
        description="Comma-separated list of allowed CORS origins. Must be explicit in production.",
    )
    enforce_https: bool = Field(
        default=False,
        description="Redirect HTTP to HTTPS and add HSTS header. Auto-enabled in production.",
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
            "║    \"import base64,os;                                       ║",
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
