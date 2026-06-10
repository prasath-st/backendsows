"""
Email Templates management for the Super Admin Email Templates page.

  GET  /api/admin/email-templates            — list all 10 templates
  PUT  /api/admin/email-templates/{id}       — update subject/bodyHtml/headerColor/etc
  POST /api/admin/email-templates/{id}/reset — reset a template to its built-in default

Table: email_templates (snake_case; no matching Prisma PascalCase table)
  id             TEXT PRIMARY KEY
  name           TEXT NOT NULL
  description    TEXT NOT NULL DEFAULT ''
  subject        TEXT NOT NULL
  header_color   TEXT NOT NULL DEFAULT '#A67763'
  logo_url       TEXT NOT NULL DEFAULT 'https://glimmora.com/logo.png'
  body_html      TEXT NOT NULL
  footer_text    TEXT NOT NULL DEFAULT ''
  is_active      BOOLEAN NOT NULL DEFAULT TRUE
  last_edited_at TIMESTAMPTZ NOT NULL DEFAULT now()
  variables      JSONB NOT NULL DEFAULT '[]'

Response shape (camelCase) mirrors the FE EmailTemplate TS interface:
  { id, name, description, subject, headerColor, logoUrl, bodyHtml,
    footerText, isActive, lastEditedAt, variables[] }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from psycopg2.extras import Json, RealDictCursor
from pydantic import BaseModel

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["superadmin-email-templates"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _row_to_out(row: dict[str, Any]) -> dict[str, Any]:
    """Map a DB row (snake_case) → FE shape (camelCase)."""
    variables = row.get("variables") or []
    if isinstance(variables, str):
        try:
            variables = json.loads(variables)
        except (ValueError, TypeError):
            variables = []
    return {
        "id": row["id"],
        "name": row.get("name") or "",
        "description": row.get("description") or "",
        "subject": row.get("subject") or "",
        "headerColor": row.get("header_color") or "#A67763",
        "logoUrl": row.get("logo_url") or "https://glimmora.com/logo.png",
        "bodyHtml": row.get("body_html") or "",
        "footerText": row.get("footer_text") or "",
        "isActive": bool(row.get("is_active", True)),
        "lastEditedAt": _iso(row.get("last_edited_at")),
        "variables": variables,
    }


# ── DDL + seed ────────────────────────────────────────────────────────────────

_DDL_DONE = False

_DEFAULT_FOOTER = (
    "© Glimmora Technologies Pvt. Ltd. · "
    "You received this because you are part of an active SOW workflow."
)
_DEFAULT_LOGO = "https://glimmora.com/logo.png"
_DEFAULT_COLOR = "#A67763"

_SEED_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "sow_stage_activated",
        "name": "SOW Stage Activated",
        "description": "Sent to the assigned approver when their review stage begins.",
        "subject": 'Action Required: {{stageName}} review for "{{sowTitle}}"',
        "header_color": _DEFAULT_COLOR,
        "logo_url": _DEFAULT_LOGO,
        "body_html": (
            "<p>Hi {{approverName}},</p>\n"
            "<p>The <strong>{{stageName}}</strong> approval stage for the SOW "
            "<strong>\"{{sowTitle}}\"</strong> has been activated and is now awaiting your review.</p>\n"
            "<p>Please complete your review by <strong>{{slaDeadline}}</strong> "
            "to keep the approval pipeline on track.</p>\n"
            "<p><a href=\"{{sowUrl}}\">Review SOW →</a></p>\n"
            "<p>If you have questions, reply to this email or reach out to your designated contact.</p>"
        ),
        "footer_text": _DEFAULT_FOOTER,
        "is_active": True,
        "variables": ["approverName", "stageName", "sowTitle", "slaDeadline", "sowUrl"],
    },
    {
        "id": "sow_stage_approved",
        "name": "SOW Stage Approved",
        "description": "Sent to the submitter (and optionally the next approver) when a stage is approved.",
        "subject": '{{stageName}} approved for "{{sowTitle}}"',
        "header_color": "#2D6A4F",
        "logo_url": _DEFAULT_LOGO,
        "body_html": (
            "<p>Hi {{recipientName}},</p>\n"
            "<p>Great news! The <strong>{{stageName}}</strong> stage for "
            "<strong>\"{{sowTitle}}\"</strong> has been approved by "
            "<strong>{{approverName}}</strong>.</p>\n"
            "{{#nextStageName}}<p>The SOW has automatically advanced to the "
            "<strong>{{nextStageName}}</strong> stage.</p>{{/nextStageName}}\n"
            "<p><a href=\"{{sowUrl}}\">View SOW status →</a></p>"
        ),
        "footer_text": _DEFAULT_FOOTER,
        "is_active": True,
        "variables": ["recipientName", "stageName", "approverName", "nextStageName", "sowTitle", "sowUrl"],
    },
    {
        "id": "sow_changes_requested",
        "name": "Changes Requested",
        "description": "Sent to the submitter when a reviewer requests changes.",
        "subject": 'Changes requested on "{{sowTitle}}" — {{stageName}}',
        "header_color": "#92400E",
        "logo_url": _DEFAULT_LOGO,
        "body_html": (
            "<p>Hi {{recipientName}},</p>\n"
            "<p>The reviewer for the <strong>{{stageName}}</strong> stage has requested "
            "changes to the SOW <strong>\"{{sowTitle}}\"</strong>.</p>\n"
            "<p><strong>Reason:</strong> {{reason}}</p>\n"
            "<p>Please address the feedback and resubmit for review.</p>\n"
            "<p><a href=\"{{sowUrl}}\">View &amp; Update SOW →</a></p>"
        ),
        "footer_text": _DEFAULT_FOOTER,
        "is_active": True,
        "variables": ["recipientName", "stageName", "reason", "sowTitle", "sowUrl"],
    },
    {
        "id": "sow_fully_approved",
        "name": "SOW Fully Approved",
        "description": "Sent to the enterprise admin when all 5 approval stages are complete.",
        "subject": 'SOW Approved ✓ — "{{sowTitle}}"',
        "header_color": "#1E40AF",
        "logo_url": _DEFAULT_LOGO,
        "body_html": (
            "<p>Hi {{adminName}},</p>\n"
            "<p>All five approval stages for <strong>\"{{sowTitle}}\"</strong> have been "
            "successfully completed.</p>\n"
            "<p>The SOW was fully approved on <strong>{{approvedAt}}</strong> and is now "
            "ready for project decomposition and team formation.</p>\n"
            "<p><a href=\"{{sowUrl}}\">View Approved SOW →</a></p>"
        ),
        "footer_text": _DEFAULT_FOOTER,
        "is_active": True,
        "variables": ["adminName", "sowTitle", "approvedAt", "sowUrl"],
    },
    {
        "id": "welcome_contributor",
        "name": "Welcome — Contributor",
        "description": "Sent to a new contributor after successful registration.",
        "subject": "Welcome to Glimmora, {{firstName}}!",
        "header_color": "#0F766E",
        "logo_url": _DEFAULT_LOGO,
        "body_html": (
            "<p>Hi {{firstName}},</p>\n"
            "<p>Welcome to <strong>Glimmora</strong> — the AI-Governed Global Workforce Platform. "
            "Your contributor account has been created successfully.</p>\n"
            "<p>To get started, complete your profile and explore available tasks:</p>\n"
            "<p><a href=\"{{onboardingUrl}}\">Complete Onboarding →</a></p>\n"
            "<p>Already done? <a href=\"{{loginUrl}}\">Log in to your dashboard</a>.</p>\n"
            "<p>We're excited to have you on board!</p>"
        ),
        "footer_text": (
            "© Glimmora Technologies Pvt. Ltd. · "
            "You received this because you registered as a contributor."
        ),
        "is_active": True,
        "variables": ["firstName", "loginUrl", "onboardingUrl"],
    },
    {
        "id": "welcome_enterprise",
        "name": "Welcome — Enterprise Admin",
        "description": "Sent to a new enterprise admin after successful organization registration.",
        "subject": "Welcome to Glimmora — {{orgName}} is ready",
        "header_color": _DEFAULT_COLOR,
        "logo_url": _DEFAULT_LOGO,
        "body_html": (
            "<p>Hi {{firstName}},</p>\n"
            "<p>Your enterprise organization <strong>{{orgName}}</strong> has been successfully "
            "registered on <strong>Glimmora</strong>.</p>\n"
            "<p>You can now upload SOWs, form teams, and manage your entire project delivery "
            "lifecycle from your admin console:</p>\n"
            "<p><a href=\"{{dashboardUrl}}\">Go to Enterprise Dashboard →</a></p>\n"
            "<p>Need help? Reply to this email or visit our support centre.</p>"
        ),
        "footer_text": (
            "© Glimmora Technologies Pvt. Ltd. · "
            "You received this because you registered an enterprise organization."
        ),
        "is_active": True,
        "variables": ["firstName", "orgName", "dashboardUrl"],
    },
    {
        "id": "welcome_reviewer",
        "name": "Welcome — Reviewer",
        "description": "Sent to a newly onboarded reviewer with login credentials and a prompt to reset their password.",
        "subject": "Welcome to GlimmoraTeam, {{firstName}} — Your reviewer account is ready",
        "header_color": "#4D5741",
        "logo_url": _DEFAULT_LOGO,
        "body_html": (
            "<p>Hi <strong>{{firstName}}</strong>,</p>\n"
            "<p>Welcome to <strong>GlimmoraTeam</strong>! Your reviewer account has been created.</p>\n"
            "<p>Login: <strong>{{loginEmail}}</strong> / Temp password: <strong>{{tempPassword}}</strong></p>\n"
            "<p>You will be prompted to reset your password on first login.</p>\n"
            "<p style=\"text-align:center;margin:28px 0;\">"
            "<a href=\"{{dashboardUrl}}\" style=\"display:inline-block;background:#4D5741;"
            "color:#ffffff;font-size:15px;font-weight:700;text-decoration:none;"
            "border-radius:10px;padding:14px 32px;\">Log In &amp; Reset Password →</a></p>"
        ),
        "footer_text": (
            "© Glimmora Technologies Pvt. Ltd. · "
            "You received this because you were onboarded as a reviewer on GlimmoraTeam."
        ),
        "is_active": True,
        "variables": ["firstName", "loginEmail", "tempPassword", "orgName", "dashboardUrl", "supportUrl"],
    },
    {
        "id": "otp_email",
        "name": "Email Verification OTP",
        "description": "Sent when a user requests an email verification code.",
        "subject": "Your GlimmoraTeam verification code",
        "header_color": "#007A8A",
        "logo_url": _DEFAULT_LOGO,
        "body_html": (
            "<p>Hi there,</p>\n"
            "<p>Your one-time verification code is valid for "
            "<strong>{{expiryMinutes}} minutes</strong>. Do not share this code with anyone.</p>\n"
            "<p style=\"text-align:center;font-size:44px;font-weight:800;letter-spacing:16px;"
            "color:#0D1B2A;font-family:'Courier New',Courier,monospace;\">{{code}}</p>\n"
            "<p style=\"text-align:center;font-size:12px;color:#6b7280;\">Expires in {{expiryMinutes}} minutes</p>"
        ),
        "footer_text": (
            "You received this because you requested email verification on GlimmoraTeam. "
            "© Glimmora Technologies Pvt. Ltd."
        ),
        "is_active": True,
        "variables": ["code", "expiryMinutes"],
    },
    {
        "id": "reviewer_invitation",
        "name": "Reviewer Invitation",
        "description": "Sent when a tenant admin invites a reviewer — signup link only (they choose their password).",
        "subject": "You've been invited as a Reviewer on GlimmoraTeam",
        "header_color": "#5B3A29",
        "logo_url": _DEFAULT_LOGO,
        "body_html": (
            "<p>Hi <strong>{{reviewerName}}</strong>,</p>\n"
            "<p><strong>{{inviterName}}</strong> from <strong>{{inviterOrg}}</strong> has invited you "
            "to join as a <strong>QA Reviewer</strong> on GlimmoraTeam.</p>\n"
            "{{personalNote}}\n"
            "<p>Create your account using the email address this message was sent to "
            "(<strong>{{loginEmail}}</strong>).</p>\n"
            "<p style=\"text-align:center;margin:28px 0;\">"
            "<a href=\"{{registerUrl}}\" style=\"display:inline-block;background:#5B3A29;"
            "color:#ffffff;font-size:15px;font-weight:700;text-decoration:none;"
            "border-radius:10px;padding:14px 32px;\">Create your reviewer account →</a></p>\n"
            "<p style=\"background:#F4F5F3;border-radius:10px;padding:14px 20px;"
            "font-size:13px;color:#374151;\">This invitation expires in "
            "<strong>{{expiryDays}} days</strong>.</p>"
        ),
        "footer_text": (
            "© Glimmora Technologies Pvt. Ltd. · "
            "You received this because you were invited as a reviewer on GlimmoraTeam."
        ),
        "is_active": True,
        "variables": ["reviewerName", "inviterName", "inviterOrg", "loginEmail", "registerUrl", "expiryDays", "personalNote"],
    },
    {
        "id": "forgot_password",
        "name": "Forgot Password",
        "description": "Sent to the user when they request a password reset link.",
        "subject": "Reset your GlimmoraTeam password",
        "header_color": _DEFAULT_COLOR,
        "logo_url": _DEFAULT_LOGO,
        "body_html": (
            "<p>Hi {{userName}},</p>\n"
            "<p>We received a request to reset the password for your GlimmoraTeam account.</p>\n"
            "<p>Click the button below to set a new password. This link is valid for "
            "<strong>{{expiryMinutes}} minutes</strong>.</p>\n"
            "<p style=\"text-align:center;margin:28px 0;\">"
            "<a href=\"{{resetLink}}\" style=\"display:inline-block;background:#A67763;"
            "color:#ffffff;font-size:15px;font-weight:700;text-decoration:none;"
            "border-radius:10px;padding:14px 32px;\">Reset My Password →</a></p>\n"
            "<p>If you did not request a password reset, you can safely ignore this email. "
            "Your password will not be changed.</p>"
        ),
        "footer_text": (
            "© Glimmora Technologies Pvt. Ltd. · "
            "You received this because a password reset was requested for your account."
        ),
        "is_active": True,
        "variables": ["userName", "resetLink", "expiryMinutes"],
    },
]


def init_email_templates_schema() -> None:
    """Create the email_templates table and seed default rows. Idempotent."""
    global _DDL_DONE
    if _DDL_DONE:
        return
    _DDL_DONE = True

    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS email_templates (
                id             TEXT PRIMARY KEY,
                name           TEXT NOT NULL DEFAULT '',
                description    TEXT NOT NULL DEFAULT '',
                subject        TEXT NOT NULL DEFAULT '',
                header_color   TEXT NOT NULL DEFAULT '#A67763',
                logo_url       TEXT NOT NULL DEFAULT 'https://glimmora.com/logo.png',
                body_html      TEXT NOT NULL DEFAULT '',
                footer_text    TEXT NOT NULL DEFAULT '',
                is_active      BOOLEAN NOT NULL DEFAULT TRUE,
                last_edited_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                variables      JSONB NOT NULL DEFAULT '[]'
            )
            """
        )
    conn.commit()

    # Seed default rows (ON CONFLICT DO NOTHING = idempotent)
    with conn.cursor() as cur:
        for tpl in _SEED_TEMPLATES:
            cur.execute(
                """
                INSERT INTO email_templates
                    (id, name, description, subject, header_color, logo_url,
                     body_html, footer_text, is_active, last_edited_at, variables)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    tpl["id"],
                    tpl["name"],
                    tpl.get("description", ""),
                    tpl["subject"],
                    tpl["header_color"],
                    tpl["logo_url"],
                    tpl["body_html"],
                    tpl["footer_text"],
                    tpl["is_active"],
                    datetime.now(timezone.utc),
                    Json(tpl["variables"]),
                ),
            )
    conn.commit()
    logger.info("email_templates table ensured + seeded")


# Run DDL at import time so the table is ready before the first request.
try:
    init_email_templates_schema()
except Exception as _exc:  # noqa: BLE001
    logger.warning("email_templates schema init deferred (DB not available at import): %s", _exc)
    _DDL_DONE = False  # allow retry on first real request


# ── Pydantic models ───────────────────────────────────────────────────────────

class EmailTemplateUpdate(BaseModel):
    """Patch body for PUT /api/admin/email-templates/{id}.
    All fields optional — only supplied fields are updated."""
    subject: str | None = None
    bodyHtml: str | None = None
    headerColor: str | None = None
    logoUrl: str | None = None
    footerText: str | None = None
    isActive: bool | None = None
    name: str | None = None
    description: str | None = None
    variables: list[str] | None = None

    model_config = {"extra": "ignore"}


# ── GET /api/admin/email-templates ────────────────────────────────────────────

@router.get("/api/admin/email-templates")
async def list_email_templates(
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Return all email templates as a flat list (matches FE EmailTemplate[])."""
    init_email_templates_schema()
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM email_templates ORDER BY id"
        )
        rows = cur.fetchall()
    return [_row_to_out(dict(r)) for r in rows]


# ── PUT /api/admin/email-templates/{template_id} ──────────────────────────────

@router.put("/api/admin/email-templates/{template_id}")
async def update_email_template(
    template_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: EmailTemplateUpdate = Body(...),
):
    """Update mutable fields of an email template. Returns the updated template."""
    init_email_templates_schema()
    conn = _conn()

    # Verify template exists
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM email_templates WHERE id = %s", (template_id,))
        existing = cur.fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail={"error": "template_not_found", "id": template_id})

    # Build SET clauses for provided fields only
    updates: dict[str, Any] = {}
    if body.subject is not None:
        updates["subject"] = body.subject
    if body.bodyHtml is not None:
        updates["body_html"] = body.bodyHtml
    if body.headerColor is not None:
        updates["header_color"] = body.headerColor
    if body.logoUrl is not None:
        updates["logo_url"] = body.logoUrl
    if body.footerText is not None:
        updates["footer_text"] = body.footerText
    if body.isActive is not None:
        updates["is_active"] = body.isActive
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if body.variables is not None:
        updates["variables"] = Json(body.variables)

    if not updates:
        return _row_to_out(dict(existing))

    updates["last_edited_at"] = datetime.now(timezone.utc)

    set_clause = ", ".join(f"{col} = %s" for col in updates.keys())
    values = list(updates.values()) + [template_id]

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"UPDATE email_templates SET {set_clause} WHERE id = %s RETURNING *",
            values,
        )
        updated = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"),
            actor_email=admin.get("email"),
            actor_role=admin.get("role"),
            action="update_email_template",
            target="email_templates",
            target_id=template_id,
            service="superadmin-service",
        )
    except Exception:  # noqa: BLE001
        pass

    return _row_to_out(dict(updated))


# ── POST /api/admin/email-templates/{template_id}/reset ──────────────────────

@router.post("/api/admin/email-templates/{template_id}/reset")
async def reset_email_template(
    template_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Reset a template back to its built-in default values."""
    init_email_templates_schema()

    # Find the built-in default for this template id
    default_tpl = next((t for t in _SEED_TEMPLATES if t["id"] == template_id), None)
    if default_tpl is None:
        raise HTTPException(status_code=404, detail={"error": "template_not_found", "id": template_id})

    conn = _conn()

    # Upsert: restore defaults (or insert if somehow missing)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO email_templates
                (id, name, description, subject, header_color, logo_url,
                 body_html, footer_text, is_active, last_edited_at, variables)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name           = EXCLUDED.name,
                description    = EXCLUDED.description,
                subject        = EXCLUDED.subject,
                header_color   = EXCLUDED.header_color,
                logo_url       = EXCLUDED.logo_url,
                body_html      = EXCLUDED.body_html,
                footer_text    = EXCLUDED.footer_text,
                is_active      = EXCLUDED.is_active,
                last_edited_at = EXCLUDED.last_edited_at,
                variables      = EXCLUDED.variables
            RETURNING *
            """,
            (
                default_tpl["id"],
                default_tpl["name"],
                default_tpl.get("description", ""),
                default_tpl["subject"],
                default_tpl["header_color"],
                default_tpl["logo_url"],
                default_tpl["body_html"],
                default_tpl["footer_text"],
                default_tpl["is_active"],
                datetime.now(timezone.utc),
                Json(default_tpl["variables"]),
            ),
        )
        restored = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"),
            actor_email=admin.get("email"),
            actor_role=admin.get("role"),
            action="reset_email_template",
            target="email_templates",
            target_id=template_id,
            service="superadmin-service",
        )
    except Exception:  # noqa: BLE001
        pass

    return _row_to_out(dict(restored))
