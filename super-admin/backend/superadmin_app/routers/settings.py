"""
Settings — contributor pricing + SMTP email configuration.

  GET  /api/v1/settings/contributor-pricing   (superadmin | enterprise_admin)
  PUT  /api/v1/settings/contributor-pricing   (superadmin only)
  GET  /api/v1/config/contributor-pricing      (PUBLIC — no auth)

  GET  /api/admin/email-settings/smtp          (superadmin) — returns stored SMTP config; password masked
  PUT  /api/admin/email-settings/smtp          (superadmin) — upsert SMTP config
  POST /api/admin/email-settings/smtp/test     (superadmin) — send a test email via shared.mailer.send_email

SMTP stored as platform_settings.data['email_smtp'] (JSONB).
Shape mirrors the FE SMTPConfig:
  { provider, host, port, username, password, fromAddress, fromName,
    replyToAddress, useTLS, useSSL, active, lastTested }
"""

from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg2.extras import Json, RealDictCursor
from pydantic import BaseModel

from shared.audit import write_audit
from shared.db import get_pg_connection, ensure_pg_clean
from shared.deps import get_current_user, get_current_superadmin

from superadmin_app import repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["superadmin-settings"])

_SMTP_WRITE_ROLES = {"superadmin", "super_admin"}

# ── ensure email_settings DDL (idempotent; uses platform_settings which already exists) ──────

_EMAIL_SETTINGS_DDL_DONE = False


def _ensure_email_settings_ddl() -> None:
    """platform_settings already exists from shared.init_schema — nothing to add."""
    global _EMAIL_SETTINGS_DDL_DONE
    if _EMAIL_SETTINGS_DDL_DONE:
        return
    _EMAIL_SETTINGS_DDL_DONE = True  # platform_settings created by init_schema

_PRICING_READ_ROLES = {"superadmin", "super_admin", "enterprise_admin", "enterprise", "admin"}
_PRICING_WRITE_ROLES = {"superadmin", "super_admin"}

# ── SMTP helpers ──────────────────────────────────────────────────────────────

_SMTP_PLATFORM_KEY = "email_smtp"


def _get_smtp_config_raw() -> dict[str, Any]:
    """Return the raw SMTP config from platform_settings.data['email_smtp']. May be None."""
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT data FROM platform_settings WHERE id = 1")
        row = cur.fetchone()
    if not row:
        return {}
    data = row.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            data = {}
    return data.get(_SMTP_PLATFORM_KEY) or {}


def _set_smtp_config_raw(cfg: dict[str, Any]) -> None:
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO platform_settings (id, data)
            VALUES (1, jsonb_build_object(%s::text, %s::jsonb))
            ON CONFLICT (id) DO UPDATE
                SET data = platform_settings.data || jsonb_build_object(%s::text, %s::jsonb)
            """,
            (_SMTP_PLATFORM_KEY, Json(cfg), _SMTP_PLATFORM_KEY, Json(cfg)),
        )
    conn.commit()


def _mask_smtp(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with the password replaced by a mask."""
    masked = dict(cfg)
    if masked.get("password"):
        masked["password"] = "••••••••"
    return masked


class StudentPricing(BaseModel):
    currency: str | None = None
    hourlyRate: float | None = None


class WorkforceSlab(BaseModel):
    id: str | None = None
    minYears: float | None = None
    maxYears: float | None = None
    currency: str | None = None
    rate: float | None = None


class ContributorPricing(BaseModel):
    student: StudentPricing | None = None
    workforceSlabs: list[WorkforceSlab] | None = None


# ── GET (read roles) ──────────────────────────────────────────────────────────

@router.get("/api/v1/settings/contributor-pricing")
async def get_contributor_pricing(user: Annotated[dict, Depends(get_current_user)]):
    if (user.get("role") or "").lower() not in _PRICING_READ_ROLES:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return {"data": repo.get_contributor_pricing()}


# ── PUT (superadmin only) ─────────────────────────────────────────────────────

@router.put("/api/v1/settings/contributor-pricing")
async def put_contributor_pricing(
    body: ContributorPricing,
    request: Request,
    user: Annotated[dict, Depends(get_current_user)],
):
    if (user.get("role") or "").lower() not in _PRICING_WRITE_ROLES:
        raise HTTPException(status_code=403, detail="Super-admin access required")
    data: dict[str, Any] = body.model_dump(exclude_none=False)
    saved = repo.set_contributor_pricing(data)
    write_audit(actor_id=user.get("id"), actor_email=user.get("email"), actor_role=user.get("role"),
                action="update_contributor_pricing", service="superadmin-service",
                ip_address=request.client.host if request.client else None)
    return {"data": saved}


# ── GET public config (NO auth) ───────────────────────────────────────────────

@router.get("/api/v1/config/contributor-pricing")
async def public_contributor_pricing():
    return {"data": repo.get_contributor_pricing()}


# ────────────────────────────────────────────────────────────────────────────
# SMTP email-settings endpoints
# ────────────────────────────────────────────────────────────────────────────

class SMTPConfig(BaseModel):
    provider: str | None = None          # office365 | gmail | sendgrid | custom
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None          # plain on PUT; masked on GET
    fromAddress: str | None = None
    fromName: str | None = None
    replyToAddress: str | None = None
    useTLS: bool | None = None
    useSSL: bool | None = None
    active: bool | None = None
    lastTested: str | None = None


class SMTPTestRequest(BaseModel):
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    fromAddress: str | None = None
    fromName: str | None = None
    replyToAddress: str | None = None
    useTLS: bool | None = None
    useSSL: bool | None = None
    # Allow the full SMTPConfig shape too — extra fields ignored via model_config
    provider: str | None = None
    active: bool | None = None
    lastTested: str | None = None

    model_config = {"extra": "ignore"}


# ── GET /api/admin/email-settings/smtp ───────────────────────────────────────

@router.get("/api/admin/email-settings/smtp")
async def get_smtp_config(user: Annotated[dict, Depends(get_current_user)]):
    if (user.get("role") or "").lower() not in _SMTP_WRITE_ROLES:
        raise HTTPException(status_code=403, detail="Super-admin access required")
    _ensure_email_settings_ddl()
    cfg = _get_smtp_config_raw()
    return {"config": _mask_smtp(cfg)}


# ── PUT /api/admin/email-settings/smtp ───────────────────────────────────────

@router.put("/api/admin/email-settings/smtp")
async def put_smtp_config(
    body: SMTPConfig,
    request: Request,
    user: Annotated[dict, Depends(get_current_user)],
):
    if (user.get("role") or "").lower() not in _SMTP_WRITE_ROLES:
        raise HTTPException(status_code=403, detail="Super-admin access required")
    _ensure_email_settings_ddl()

    # Merge with existing so a blank password field doesn't erase the stored one.
    existing = _get_smtp_config_raw()
    incoming: dict[str, Any] = body.model_dump(exclude_none=True)

    # If the FE sends the mask placeholder, keep the stored password unchanged.
    if incoming.get("password") in ("••••••••", ""):
        incoming.pop("password", None)

    merged = {**existing, **incoming}
    _set_smtp_config_raw(merged)

    write_audit(
        actor_id=user.get("id"),
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="update_smtp_config",
        target="platform_settings",
        target_id="1",
        service="superadmin-service",
        ip_address=request.client.host if request.client else None,
    )
    return {"success": True, "config": _mask_smtp(merged)}


# ── POST /api/admin/email-settings/smtp/test ─────────────────────────────────

@router.post("/api/admin/email-settings/smtp/test")
async def test_smtp_config(
    body: SMTPTestRequest,
    request: Request,
    user: Annotated[dict, Depends(get_current_user)],
):
    if (user.get("role") or "").lower() not in _SMTP_WRITE_ROLES:
        raise HTTPException(status_code=403, detail="Super-admin access required")
    _ensure_email_settings_ddl()

    # Use provided values; fall back to stored config for anything not in request.
    stored = _get_smtp_config_raw()
    req_data: dict[str, Any] = body.model_dump(exclude_none=True)

    host = req_data.get("host") or stored.get("host") or ""
    port = req_data.get("port") or stored.get("port") or 587
    username = req_data.get("username") or stored.get("username") or ""
    password = req_data.get("password") or stored.get("password") or ""
    from_address = req_data.get("fromAddress") or stored.get("fromAddress") or username
    from_name = req_data.get("fromName") or stored.get("fromName") or "Glimmora"
    use_tls = req_data.get("useTLS") if req_data.get("useTLS") is not None else stored.get("useTLS", True)
    to_email = user.get("email") or ""

    # Skip the mask placeholder — treat as missing.
    if password in ("••••••••", ""):
        password = stored.get("password") or ""

    if not host or not username or not password:
        return {"ok": False, "sent": False, "message": "Incomplete SMTP config — host, username, and password are required."}

    try:
        msg = EmailMessage()
        msg["Subject"] = "Glimmora SMTP test"
        msg["From"] = f"{from_name} <{from_address}>" if from_name else from_address
        msg["To"] = to_email or from_address
        msg.set_content(
            "This is a test email sent from the Glimmora super-admin SMTP settings panel.\n\n"
            "If you received this, your SMTP configuration is working correctly."
        )

        with smtplib.SMTP(host, int(port), timeout=10) as smtp:
            if use_tls:
                smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(msg)

        write_audit(
            actor_id=user.get("id"),
            actor_email=user.get("email"),
            actor_role=user.get("role"),
            action="test_smtp_config",
            target="platform_settings",
            target_id="1",
            service="superadmin-service",
            ip_address=request.client.host if request.client else None,
        )
        return {"ok": True, "sent": True, "message": f"Test email sent to {to_email or from_address}."}

    except smtplib.SMTPAuthenticationError as exc:
        logger.warning("SMTP test auth failed: %s", exc)
        return {"ok": False, "sent": False, "message": f"Authentication failed: {exc.smtp_error.decode(errors='replace') if hasattr(exc, 'smtp_error') else str(exc)}"}
    except smtplib.SMTPException as exc:
        logger.warning("SMTP test failed: %s", exc)
        return {"ok": False, "sent": False, "message": f"SMTP error: {exc}"}
    except OSError as exc:
        logger.warning("SMTP test connection error: %s", exc)
        return {"ok": False, "sent": False, "message": f"Connection error: {exc}"}
