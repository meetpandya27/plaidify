"""Outbound email over SMTP (password-reset and sign-up messages).

Configured with SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD and
SMTP_FROM. The connection is upgraded with STARTTLS (certificate verified
against the system roots) before logging in or sending, unless
SMTP_STARTTLS=false for a local relay. Nothing here logs a message body: it
can carry a one-time token.
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


def _token_link(template: str | None, token: str) -> str | None:
    """``template`` with ``{token}`` filled in (or a ``token`` query parameter added), if it is set."""
    if not template:
        return None
    encoded = quote(token, safe="")
    if "{token}" in template:
        return template.replace("{token}", encoded)
    return f"{template}{'&' if '?' in template else '?'}token={encoded}"


def password_reset_link(token: str) -> str | None:
    """The reset page URL for ``token`` (PASSWORD_RESET_URL with ``{token}`` filled in), if configured."""
    return _token_link(settings.password_reset_url, token)


def email_verification_link(token: str) -> str | None:
    """The sign-up page URL for ``token`` (EMAIL_VERIFICATION_URL with ``{token}`` filled in), if configured."""
    return _token_link(settings.email_verification_url, token)


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


def send_sign_up_verification(to_address: str, token: str, *, username: str, expires_hours: int) -> None:
    """Email the one-time token that finishes a sign-up (as a link when EMAIL_VERIFICATION_URL is set).

    The username is named so that someone who did not start this sign-up can
    tell it is not theirs. The token does not create the account: it has to be
    sent with the password chosen at sign-up, which only that person knows.
    """
    link = email_verification_link(token)
    how = (
        f"Open this link and enter the password from that sign-up to finish signing up:\n\n    {link}\n"
        if link
        else f"Use this one-time code with the password from that sign-up to finish signing up:\n\n    {token}\n"
    )
    body = (
        f'Someone asked to create a {settings.app_name} account with the username "{username}" for this '
        "email address.\n\n"
        f"{how}\n"
        "The link or the code alone does not create the account. It works once and expires in "
        f"{expires_hours} hours. If you did not ask for this, do nothing: no account is created.\n"
    )
    send_email(to_address, f"Finish signing up for {settings.app_name}", body)


def send_sign_up_address_in_use(to_address: str) -> None:
    """Tell an account's address that someone tried to sign up with it; nothing was created."""
    body = (
        f"Someone tried to create a {settings.app_name} account with this email address, which already has "
        "an account. If it was you, sign in instead, or reset your password if you have forgotten it.\n\n"
        "If it was not you, ignore this email: nothing has changed.\n"
    )
    send_email(to_address, f"Sign-up attempt with your {settings.app_name} address", body)


def send_sign_up_username_taken(to_address: str) -> None:
    """Tell a new address that the username its sign-up asked for is taken; nothing was created."""
    body = (
        f"Someone asked to create a {settings.app_name} account for this email address, but the username "
        "they chose is taken. To sign up, register again with another username.\n\n"
        "If you did not ask for this, ignore this email.\n"
    )
    send_email(to_address, f"Choose another {settings.app_name} username", body)
