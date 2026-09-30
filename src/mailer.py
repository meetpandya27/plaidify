"""Outbound email over SMTP (password-reset messages).

Configured with SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD and
SMTP_FROM. The connection is upgraded with STARTTLS (certificate verified
against the system roots) before logging in or sending, unless
SMTP_STARTTLS=false for a local relay. Nothing here logs a message body: it
can carry a one-time reset token.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote

from src.config import get_settings
from src.logging_config import get_logger

settings = get_settings()
logger = get_logger("mailer")


class MailNotConfiguredError(RuntimeError):
    """SMTP_HOST / SMTP_FROM are not set, so no email can be sent."""


def mail_configured() -> bool:
    """Whether outbound email is set up (SMTP_HOST and SMTP_FROM)."""
    return bool(settings.smtp_host and settings.smtp_from)


def send_email(to_address: str, subject: str, body: str) -> None:
    """Send one plain-text message.

    Raises:
        MailNotConfiguredError: SMTP is not configured.
        smtplib.SMTPException / OSError: the server refused or could not be reached.
    """
    if not mail_configured():
        raise MailNotConfiguredError("SMTP_HOST and SMTP_FROM must be set to send email.")

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds) as smtp:
        smtp.ehlo()
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password or "")
        smtp.send_message(message)


def password_reset_link(token: str) -> str | None:
    """The reset page URL for ``token`` (PASSWORD_RESET_URL with ``{token}`` filled in), if configured."""
    template = settings.password_reset_url
    if not template:
        return None
    encoded = quote(token, safe="")
    if "{token}" in template:
        return template.replace("{token}", encoded)
    return f"{template}{'&' if '?' in template else '?'}token={encoded}"


def send_password_reset_email(to_address: str, token: str, *, expires_minutes: int) -> None:
    """Email a one-time password-reset token (as a link when PASSWORD_RESET_URL is set)."""
    link = password_reset_link(token)
    how = (
        f"Open this link to choose a new password:\n\n    {link}\n"
        if link
        else f"Use this one-time code to choose a new password:\n\n    {token}\n"
    )
    body = (
        f"Someone asked to reset the password of your {settings.app_name} account.\n\n"
        f"{how}\n"
        f"It works once and expires in {expires_minutes} minutes. If you did not ask for this, "
        "ignore this email: your password stays as it is.\n"
    )
    send_email(to_address, f"Reset your {settings.app_name} password", body)
