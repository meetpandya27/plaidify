"""
Structured logging configuration for Plaidify.

Supports two formats:
- 'json': Machine-readable JSON logs (production)
- 'text': Human-readable colored logs (development)
"""

import json
import logging
import re
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode

# Query parameters whose values must never reach a log line: site credentials,
# MFA codes and sessions, link/access/consent tokens, and OAuth callback values.
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "username",
        "password",
        "encrypted_username",
        "encrypted_password",
        "code",
        "session_id",
        "token",
        "link_token",
        "public_token",
        "access_token",
        "refresh_token",
        "consent_token",
        "state",
    }
)


# Routes whose path segment is itself a bearer credential: access and link
# tokens, consent tokens, MFA sessions, and job ids (anonymous jobs are read by
# id alone). Static siblings (/link/sessions/bootstrap, /consent/request) stay.
_SECRET_PATH_SEGMENTS = (
    re.compile(r"^(/(?:access_jobs|links|tokens|link/events|encryption/public_key|mfa/status|refresh/schedule)/)[^/]+"),
    re.compile(r"^(/link/sessions/)(?!(?:bootstrap|public)$)[^/]+"),
    re.compile(r"^(/consent/)(?!request$)[^/]+$"),
)


def redact_path(path: str) -> str:
    """Blank credentials carried in the path itself (e.g. ``DELETE /tokens/{token}``)."""
    for pattern in _SECRET_PATH_SEGMENTS:
        path, count = pattern.subn(r"\1REDACTED", path, count=1)
        if count:
            break
    return path


def redact_query(path: str) -> str:
    """Blank the values of sensitive query parameters in a request path."""
    base, sep, query = path.partition("?")
    if not sep:
        return path
    pairs = [
        (key, "REDACTED" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
        for key, value in parse_qsl(query, keep_blank_values=True)
    ]
    return f"{base}?{urlencode(pairs)}"


class AccessLogRedactFilter(logging.Filter):
    """Redacts secrets from uvicorn access lines.

    uvicorn logs ``(client, method, path_with_query, http_version, status)``,
    and gunicorn's UvicornWorker re-enables that logger at INFO in every
    worker, so filtering the record is the only reliable place to do it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            base, sep, query = args[2].partition("?")
            record.args = args[:2] + (redact_query(redact_path(base) + sep + query),) + args[3:]
        return True


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add correlation ID if present
        if hasattr(record, "correlation_id"):
            log_entry["correlation_id"] = record.correlation_id

        # Add extra fields
        if hasattr(record, "extra_data"):
            log_entry.update(record.extra_data)

        # Add exception info if present
        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable colored log formatter for development."""

    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[41m",  # Red background
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prefix = f"{color}{record.levelname:8s}{self.RESET}"
        message = f"{timestamp} {prefix} [{record.name}] {record.getMessage()}"

        if hasattr(record, "correlation_id"):
            message += f" [cid={record.correlation_id}]"

        if record.exc_info and record.exc_info[1] is not None:
            message += f"\n{self.formatException(record.exc_info)}"

        return message


def setup_logging(level: str = "INFO", log_format: str = "json") -> None:
    """
    Configure the root logger for Plaidify.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_format: 'json' for structured logs, 'text' for human-readable.
    """
    root_logger = logging.getLogger()

    # Clear existing handlers
    root_logger.handlers.clear()

    # Create handler
    handler = logging.StreamHandler(sys.stdout)

    # Set formatter
    if log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(TextFormatter())

    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Silence noisy third-party loggers
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.setLevel(logging.WARNING)
    if not any(isinstance(f, AccessLogRedactFilter) for f in access_logger.filters):
        access_logger.addFilter(AccessLogRedactFilter())
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """
    Get a named logger for a module.

    Usage:
        from src.logging_config import get_logger
        logger = get_logger(__name__)
        logger.info("Something happened")
    """
    return logging.getLogger(f"plaidify.{name}")
