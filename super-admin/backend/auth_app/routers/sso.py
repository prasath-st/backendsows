"""
SSO + Reviewer-Register endpoints mounted under /api/v1/auth.

Endpoints this module provides:
  POST /api/v1/auth/oidc/{tenantSlug}/callback
      Accept OIDC claims (mock Phase-1: caller POSTs pre-parsed claims + code).
      JIT-provision a login_accounts row if first-time; create sso_sessions row.
      Return {ok, userId, sessionId, jitProvisioned}.

  POST /api/v1/auth/saml/{tenantSlug}/callback
      Accept SAML attributes (mock Phase-1: caller POSTs pre-parsed attrs).
      Same JIT-provision + session pipeline as OIDC.
      Return {ok, userId, sessionId, jitProvisioned}.

  POST /api/v1/auth/register/reviewer
      Validate inviteCode from reviewer_invites (Postgres), create reviewer
      account in login_accounts, mark invite accepted.
      Return {ok, userId, email}.

FE proxy paths:
  /api/auth/oidc/[tenantSlug]/callback  → /api/v1/auth/oidc/{tenantSlug}/callback
  /api/auth/saml/[tenantSlug]/callback  → /api/v1/auth/saml/{tenantSlug}/callback
  /api/auth/register/reviewer           → /api/v1/auth/register/reviewer
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from psycopg2.extras import Json, RealDictCursor
from pydantic import BaseModel, EmailStr

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.security import create_access_token, create_refresh_token, hash_password, access_token_seconds

router = APIRouter(prefix="/api/v1/auth", tags=["sso", "reviewer-register"])


# ── DB helpers ───────────────────────────────────────────────────────────────

def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def _load_tenant_sso(tenant_slug: str) -> dict[str, Any] | None:
    """Return tenant_sso_configs row for slug, or None."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM tenant_sso_configs WHERE slug = %s",
            (tenant_slug,),
        )
        return cur.fetchone()


def _find_account_by_email(email: str) -> dict[str, Any] | None:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM login_accounts WHERE LOWER(email) = LOWER(%s)",
            (email,),
        )
        return cur.fetchone()


def _jit_provision(
    email: str,
    first_name: str,
    last_name: str,
    role: str,
    tenant_id: str | None,
    provider: str,
) -> tuple[dict[str, Any], bool]:
    """Upsert a login_accounts row for an SSO user.

    Returns (row, jit_provisioned).  jit_provisioned is True only when this
    is the very first sign-in (we created the row).  Subsequent sign-ins
    refresh the display name + provider but do NOT overwrite an existing
    tenant_id (a user already claimed by another tenant stays there).
    """
    conn = _conn()
    existing = _find_account_by_email(email)

    if existing:
        # Refresh display fields; never overwrite existing tenant_id.
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE login_accounts
                   SET first_name = COALESCE(NULLIF(%s,''), first_name),
                       last_name  = COALESCE(NULLIF(%s,''), last_name),
                       provider   = %s,
                       tenant_id  = CASE WHEN tenant_id IS NULL THEN %s ELSE tenant_id END,
                       updated_at = now()
                 WHERE id = %s
                 RETURNING *
                """,
                (first_name, last_name, provider,
                 tenant_id,
                 existing["id"]),
            )
            row = cur.fetchone() or existing
        conn.commit()
        return row, False

    # First-time SSO user — create account (no password; is_password_set=FALSE).
    name = f"{first_name} {last_name}".strip() or email.split("@")[0]
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO login_accounts
                (email, password_hash, provider, first_name, last_name, name,
                 role, tenant_id, email_verified, is_password_set)
            VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, TRUE, FALSE)
            RETURNING *
            """,
            (email.lower(), provider,
             first_name or email.split("@")[0], last_name, name,
             role, tenant_id),
        )
        row = cur.fetchone()
    conn.commit()
    return row, True


def _create_sso_session(
    account_id: int | str,
    tenant_id: str | None,
    kind: str,
    ip_address: str | None,
    user_agent: str | None,
    ttl_days: int = 30,
) -> str:
    """Insert an sso_sessions row and return the session UUID."""
    session_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sso_sessions
                (id, account_id, tenant_id, kind, ip_address, user_agent, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (session_id, account_id, tenant_id, kind, ip_address, user_agent, expires_at),
        )
    conn.commit()
    return session_id


# ── SSO assertion parsing (mock Phase-1) ─────────────────────────────────────

def _parse_oidc_assertion(body: dict[str, Any]) -> dict[str, Any]:
    """Extract normalized claim fields from the mock OIDC POST body.

    Expected shape (mirrors FE OidcMockInput):
      {claims: {email, given_name?, family_name?, sub?}, code: str}
    """
    claims = body.get("claims") or {}
    if not claims.get("email"):
        raise ValueError("OIDC token response missing 'email' claim")
    return {
        "email": claims["email"],
        "first_name": claims.get("given_name") or "",
        "last_name": claims.get("family_name") or "",
        "subject": claims.get("sub") or claims["email"],
    }


def _parse_saml_assertion(
    sso_config_row: dict[str, Any],
    body: dict[str, Any],
) -> dict[str, Any]:
    """Extract normalized assertion fields from the mock SAML POST body.

    Expected shape (mirrors FE SamlMockInput):
      {attributes: {<key>: str, ...}, subject?: str, signaturePresent?: bool}
    """
    import json as _json

    if body.get("signaturePresent") is False:
        raise PermissionError("SAML response signature invalid")

    # Resolve the attribute map from the stored sso_config JSONB.
    raw_cfg = sso_config_row.get("sso_config") or {}
    if isinstance(raw_cfg, str):
        try:
            raw_cfg = _json.loads(raw_cfg)
        except (ValueError, TypeError):
            raw_cfg = {}

    saml_cfg = raw_cfg.get("saml") or {}
    attr_map = saml_cfg.get("attributeMap") or {}
    email_key = attr_map.get("email") or "email"
    first_key = attr_map.get("firstName") or "firstName"
    last_key = attr_map.get("lastName") or "lastName"

    attrs = body.get("attributes") or {}
    email = attrs.get(email_key)
    if not email:
        raise ValueError(f"SAML response missing required attribute '{email_key}'")

    return {
        "email": email,
        "first_name": attrs.get(first_key) or "",
        "last_name": attrs.get(last_key) or "",
        "subject": body.get("subject") or email,
    }


# ── Pydantic bodies ───────────────────────────────────────────────────────────

class OidcCallbackBody(BaseModel):
    claims: dict[str, Any]
    code: str = ""


class SamlCallbackBody(BaseModel):
    attributes: dict[str, Any] = {}
    subject: str | None = None
    signaturePresent: bool | None = None


class ReviewerRegisterBody(BaseModel):
    firstName: str
    lastName: str
    email: EmailStr
    password: str
    inviteCode: str


# ── OIDC callback ─────────────────────────────────────────────────────────────

@router.post("/oidc/{tenantSlug}/callback")
async def oidc_callback(
    tenantSlug: str,
    body: OidcCallbackBody,
    request: Request,
):
    """POST /api/v1/auth/oidc/{tenantSlug}/callback

    Accepts OIDC claims (Phase-1 mock: pre-parsed claims + mock code).
    JIT-provisions the user in login_accounts if first sign-in.
    Returns {ok, userId, sessionId, jitProvisioned}.
    """
    row = _load_tenant_sso(tenantSlug)
    if not row or not row.get("enabled") or row.get("kind") != "oidc":
        return {"error": "sso_not_configured"}, 404

    ip_address = request.headers.get("x-forwarded-for") or (
        request.client.host if request.client else None
    )
    user_agent = request.headers.get("user-agent")

    try:
        assertion = _parse_oidc_assertion(body.model_dump())
    except ValueError as exc:
        # Record signature/claim failure in audit (fail-open).
        write_audit(
            actor_id=None, actor_email=None, actor_role=None,
            action="auth.sso.oidc.signature_invalid",
            tenant_id=str(row.get("tenant_id") or ""),
            service="superadmin-service",
            ip_address=ip_address,
            extra={"tenantSlug": tenantSlug, "reason": str(exc)},
        )
        raise HTTPException(status_code=401, detail="oidc_token_invalid")

    tenant_id = str(row.get("tenant_id") or "")
    default_role = str(row.get("default_role") or "enterprise")
    provider = f"oidc-{tenantSlug}"

    account, jit = _jit_provision(
        email=assertion["email"],
        first_name=assertion["first_name"],
        last_name=assertion["last_name"],
        role=default_role,
        tenant_id=tenant_id or None,
        provider=provider,
    )

    session_id = _create_sso_session(
        account_id=account["id"],
        tenant_id=tenant_id or None,
        kind="oidc",
        ip_address=ip_address,
        user_agent=user_agent,
    )

    write_audit(
        actor_id=str(account["id"]),
        actor_email=assertion["email"],
        actor_role=account.get("role"),
        action="auth.sso.oidc.signin",
        tenant_id=tenant_id or None,
        service="superadmin-service",
        ip_address=ip_address,
        extra={
            "tenantSlug": tenantSlug,
            "kind": "oidc",
            "jitProvisioned": jit,
            "subject": assertion.get("subject"),
        },
    )

    return {
        "ok": True,
        "userId": str(account["id"]),
        "sessionId": session_id,
        "jitProvisioned": jit,
    }


# ── SAML callback ─────────────────────────────────────────────────────────────

@router.post("/saml/{tenantSlug}/callback")
async def saml_callback(
    tenantSlug: str,
    body: SamlCallbackBody,
    request: Request,
):
    """POST /api/v1/auth/saml/{tenantSlug}/callback

    Accepts SAML attributes (Phase-1 mock: pre-parsed attributes dict).
    JIT-provisions the user in login_accounts if first sign-in.
    Returns {ok, userId, sessionId, jitProvisioned}.
    """
    row = _load_tenant_sso(tenantSlug)
    if not row or not row.get("enabled") or row.get("kind") != "saml":
        return {"error": "sso_not_configured"}, 404

    ip_address = request.headers.get("x-forwarded-for") or (
        request.client.host if request.client else None
    )
    user_agent = request.headers.get("user-agent")

    try:
        assertion = _parse_saml_assertion(row, body.model_dump())
    except PermissionError as exc:
        write_audit(
            actor_id=None, actor_email=None, actor_role=None,
            action="auth.sso.saml.signature_invalid",
            tenant_id=str(row.get("tenant_id") or ""),
            service="superadmin-service",
            ip_address=ip_address,
            extra={"tenantSlug": tenantSlug, "reason": str(exc)},
        )
        raise HTTPException(status_code=401, detail="saml_signature_invalid")
    except ValueError as exc:
        write_audit(
            actor_id=None, actor_email=None, actor_role=None,
            action="auth.sso.saml.signature_invalid",
            tenant_id=str(row.get("tenant_id") or ""),
            service="superadmin-service",
            ip_address=ip_address,
            extra={"tenantSlug": tenantSlug, "reason": str(exc)},
        )
        raise HTTPException(status_code=401, detail="saml_signature_invalid")

    tenant_id = str(row.get("tenant_id") or "")
    default_role = str(row.get("default_role") or "enterprise")
    provider = f"saml-{tenantSlug}"

    account, jit = _jit_provision(
        email=assertion["email"],
        first_name=assertion["first_name"],
        last_name=assertion["last_name"],
        role=default_role,
        tenant_id=tenant_id or None,
        provider=provider,
    )

    session_id = _create_sso_session(
        account_id=account["id"],
        tenant_id=tenant_id or None,
        kind="saml",
        ip_address=ip_address,
        user_agent=user_agent,
    )

    write_audit(
        actor_id=str(account["id"]),
        actor_email=assertion["email"],
        actor_role=account.get("role"),
        action="auth.sso.saml.signin",
        tenant_id=tenant_id or None,
        service="superadmin-service",
        ip_address=ip_address,
        extra={
            "tenantSlug": tenantSlug,
            "kind": "saml",
            "jitProvisioned": jit,
            "subject": assertion.get("subject"),
        },
    )

    return {
        "ok": True,
        "userId": str(account["id"]),
        "sessionId": session_id,
        "jitProvisioned": jit,
    }


# ── Reviewer self-register ────────────────────────────────────────────────────

def _load_invite(code: str) -> dict[str, Any] | None:
    """Return reviewer_invites row for code with expiry side-effect."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM reviewer_invites WHERE code = %s",
            (code.strip(),),
        )
        row = cur.fetchone()
    if not row:
        return None
    # Mark expired rows in DB lazily.
    if row.get("status") == "pending" and row.get("expires_at"):
        exp = row["expires_at"]
        if isinstance(exp, datetime) and exp < datetime.now(timezone.utc):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE reviewer_invites SET status='expired' WHERE id = %s",
                    (row["id"],),
                )
            conn.commit()
            row = dict(row)
            row["status"] = "expired"
    return row


@router.post("/register/reviewer")
async def register_reviewer(body: ReviewerRegisterBody, request: Request):
    """POST /api/v1/auth/register/reviewer

    Validates inviteCode in reviewer_invites (Postgres), creates a reviewer
    account in login_accounts, marks the invite accepted.
    Returns {ok, userId, email}.
    """
    # 1. Load + validate invite.
    invite = _load_invite(body.inviteCode)
    if not invite:
        raise HTTPException(status_code=400, detail="This invite is invalid or has expired.")
    if invite.get("status") == "expired":
        raise HTTPException(status_code=400, detail="This invite has expired. Ask your admin to send a new one.")
    if invite.get("status") == "accepted":
        raise HTTPException(status_code=409, detail="This invite has already been used.")
    invite_email = (invite.get("email") or "").lower().strip()
    if invite_email and invite_email != body.email.lower().strip():
        raise HTTPException(
            status_code=400,
            detail="Use the same email address your admin invited.",
        )

    # 2. Duplicate email guard.
    existing = _find_account_by_email(body.email)
    if existing:
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists. Sign in instead.",
        )

    # 3. Create login_accounts row for the reviewer.
    conn = _conn()
    name = f"{body.firstName} {body.lastName}".strip()
    tenant_id = invite.get("tenant_id")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO login_accounts
                (email, password_hash, provider, first_name, last_name, name,
                 role, tenant_id, email_verified, is_password_set)
            VALUES (%s, %s, 'invite', %s, %s, %s, 'reviewer', %s, TRUE, TRUE)
            RETURNING id, email, role
            """,
            (
                body.email.lower().strip(),
                hash_password(body.password),
                body.firstName.strip(),
                body.lastName.strip(),
                name,
                tenant_id,
            ),
        )
        new_row = cur.fetchone()

    # 4. Mark invite accepted.
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE reviewer_invites
               SET status = 'accepted', accepted_at = now()
             WHERE code = %s
            """,
            (body.inviteCode.strip(),),
        )
    conn.commit()

    # 5. Audit.
    ip_address = request.headers.get("x-forwarded-for") or (
        request.client.host if request.client else None
    )
    write_audit(
        actor_id=str(new_row["id"]),
        actor_email=new_row["email"],
        actor_role="reviewer",
        action="register_reviewer",
        service="superadmin-service",
        tenant_id=tenant_id,
        ip_address=ip_address,
        extra={"inviteCode": body.inviteCode, "invitedByEmail": invite.get("invited_by_email")},
    )

    return {
        "ok": True,
        "userId": str(new_row["id"]),
        "email": new_row["email"],
    }
