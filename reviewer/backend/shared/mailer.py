"""SMTP helper shared by all services. OTP / credential emails bypass any
informational mute toggle. Includes branded HTML + credential-email builders."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Sequence

from shared.config import settings

logger = logging.getLogger(__name__)


def email_is_configured() -> bool:
    # EMAIL_ENABLED=false (cloud deploys where outbound SMTP is blocked) forces
    # email OFF so OTP / credential flows fall through to their dev-code response
    # INSTANTLY instead of hanging ~30s on an unreachable SMTP server.
    if not getattr(settings, "email_enabled", True):
        return False
    return bool(settings.email_user.strip() and settings.email_app_password_for_smtp)


def send_email(
    *,
    to_email: str,
    subject: str,
    body: str,
    html: str | None = None,
    is_otp: bool = False,
    category: str | None = None,
) -> bool:
    """Send an email over SMTP. Returns True on success."""
    if not email_is_configured():
        logger.info("Email skipped: EMAIL_USER / EMAIL_APP_PASSWORD not configured.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from_address
    msg["To"] = to_email
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=8) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            smtp.login(settings.email_user, settings.email_app_password_for_smtp)
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Email send failed to %s: %s", to_email, exc)
        return False


def send_bulk_email(
    messages: Sequence[dict[str, Any]],
    *,
    is_otp: bool = False,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Send many emails over a single SMTP connection.
    Each message: { to_email, subject, body, html? }.
    Returns [{ to_email, sent: bool }]."""
    results: list[dict[str, Any]] = []
    if not email_is_configured():
        logger.info("Bulk email skipped: EMAIL_USER / EMAIL_APP_PASSWORD not configured.")
        return [{"to_email": m.get("to_email"), "sent": False} for m in messages]

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=8) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            smtp.login(settings.email_user, settings.email_app_password_for_smtp)
            for m in messages:
                to_email = m.get("to_email")
                try:
                    msg = EmailMessage()
                    msg["Subject"] = m.get("subject", "")
                    msg["From"] = settings.email_from_address
                    msg["To"] = to_email
                    msg.set_content(m.get("body", ""))
                    if m.get("html"):
                        msg.add_alternative(m["html"], subtype="html")
                    smtp.send_message(msg)
                    results.append({"to_email": to_email, "sent": True})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Bulk email send failed to %s: %s", to_email, exc)
                    results.append({"to_email": to_email, "sent": False})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bulk email connection failed: %s", exc)
        return [{"to_email": m.get("to_email"), "sent": False} for m in messages]
    return results


# ── Credential email (used by bulk onboarding) ───────────────────────────────

def build_credentials_body(*, name: str, email: str, temp_password: str,
                           login_url: str, org_name: str | None = None) -> tuple[str, str]:
    """Return (plain_text, html) for a welcome-with-credentials email."""
    org = org_name or "Glimmora"
    text = (
        f"Hello {name},\n\n"
        f"An account has been created for you on {org}.\n\n"
        f"  Login URL : {login_url}\n"
        f"  Email     : {email}\n"
        f"  Temporary password : {temp_password}\n\n"
        "For security you'll be asked to set a new password on first login.\n\n"
        "— The Glimmora Team"
    )
    html = f"""\
<div style="font-family:Poppins,Arial,sans-serif;max-width:560px;margin:auto;color:#374151">
  <h2 style="color:#0D1B2A">Welcome to {org}</h2>
  <p>Hello <strong>{name}</strong>, an account has been created for you.</p>
  <table style="border-collapse:collapse;margin:16px 0">
    <tr><td style="padding:4px 12px;color:#6b7280">Email</td><td style="padding:4px 12px"><strong>{email}</strong></td></tr>
    <tr><td style="padding:4px 12px;color:#6b7280">Temp password</td><td style="padding:4px 12px"><strong>{temp_password}</strong></td></tr>
  </table>
  <p><a href="{login_url}" style="background:#0D1B2A;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none">Log in</a></p>
  <p style="font-size:12px;color:#9ca3af">You'll be asked to set a new password on first login.</p>
</div>"""
    return text, html
