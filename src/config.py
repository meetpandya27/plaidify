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

    # ── Encryption ────────────────────────────────────────────
    encryption_key: str = Field(
        ...,  # Required — no default
        description="Fernet encryption key for credential storage. "
        "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"",
    )

    # ── JWT / Auth ────────────────────────────────────────────
    jwt_secret_key: str = Field(
        ...,  # Required — no default
        description="Secret key for signing JWT tokens. "
        "Generate with: openssl rand -hex 32",
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT signing algorithm.")
    jwt_access_token_expire_minutes: int = Field(
        default=60 * 24 * 7,  # 1 week
        description="JWT access token expiry in minutes.",
    )

    # ── Server ────────────────────────────────────────────────
    app_name: str = Field(default="Plaidify", description="Application name.")
    app_version: str = Field(default="0.1.0", description="Application version.")
    debug: bool = Field(default=False, description="Enable debug mode.")
    log_level: str = Field(default="INFO", description="Logging level.")
    log_format: str = Field(
        default="json", description="Logging format: 'json' or 'text'."
    )
    cors_origins: str = Field(
        default="*",
        description="Comma-separated list of allowed CORS origins. Use specific origins in production.",
    )

    # ── Connectors ────────────────────────────────────────────
    connectors_dir: str = Field(
        default="connectors",
        description="Path to the directory containing connector blueprints.",
    )

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
            '║  export ENCRYPTION_KEY="$(python -c \\                       ║',
            file=sys.stderr,
        )
        print(
            "║    \"from cryptography.fernet import Fernet; \\               ║",
            file=sys.stderr,
        )
        print(
            '║    print(Fernet.generate_key().decode())")"                  ║',
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
