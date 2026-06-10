"""
Auth router — /api/v1/auth/**

Implements the contract the frontend's proxy routes expect:
  login, register/contributor, register/enterprise, refresh, logout, me,
  mfa/setup/init|confirm, mfa/verify, mfa/recovery, password/forgot|reset|change,
  otp send/verify (email+phone), validate.

Hybrid auth rule:
  * password-first user can later sign in with OAuth directly (handled in
    contributor-service, which calls back to create/link the account).
  * OAuth-first user (no password set) who tries email+password login →
    we DO NOT reject; we return status 'needs_password_setup' and require an
    OTP-verified password creation before granting tokens.
"""

from __future__ import annotations

import base64
import io
from typing import Annotated

import pyotp
import qrcode
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from shared.blob import blob_is_configured, upload_blob

from shared.audit import write_audit
from shared.config import settings
from shared.deps import get_bearer_token, get_current_user
from shared.kafka_bus import publish_event
from shared.mailer import build_credentials_body, send_email
from shared.otp import generate_code, is_recently_verified, rate_limit_ok, store_otp, verify_otp
from shared.security import (
    access_token_seconds,
    create_access_token,
    create_mfa_pending_token,
    create_refresh_token,
    create_reset_token,
    decode_token,
    generate_temp_password,
    hash_password,
    verify_password,
)

from auth_app import repo
from auth_app.schemas import (
    ChangePasswordRequest,
    ContributorRegisterRequest,
    EnterpriseRegisterRequest,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    MfaCodeRequest,
    MfaRecoveryRequest,
    OtpSendRequest,
    OtpVerifyRequest,
    RefreshRequest,
    ResetPasswordRequest,
    user_row_to_out,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _claims(row: dict) -> dict:
    return {"sub": str(row["id"]), "email": row["email"], "role": row.get("role"),
            "tenant_id": row.get("tenant_id")}


def _token_pair(row: dict) -> dict:
    access = create_access_token(_claims(row))
    refresh = create_refresh_token(_claims(row))
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": access_token_seconds(),
        "user": user_row_to_out(row).model_dump(),
    }


# ── Login ──────────────────────────────────────────────────────────────────

@router.post("/login")
async def login(body: LoginRequest, request: Request):
    if not await rate_limit_ok("login", body.email,
                               settings.rate_limit_login_requests,
                               settings.rate_limit_login_window_seconds):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    row = repo.find_account_by_email(body.email)
    if not row:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Hybrid rule: OAuth-first account with no password set.
    if not row.get("is_password_set") or not row.get("password_hash"):
        # Send an OTP so the user can verify ownership and create a password.
        code = generate_code()
        await store_otp("email", row["email"], code)
        send_email(
            to_email=row["email"],
            subject="Glimmora — verify your email to set a password",
            body=f"Your verification code is {code}. It expires in 5 minutes.",
            is_otp=True,
        )
        return {
            "status": "needs_password_setup",
            "message": "This account was created via single sign-on. "
                       "We've emailed a code to verify your email and set a password.",
            "user": {"id": str(row["id"]), "email": row["email"]},
        }

    if not body.password or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not row.get("is_active", True):
        raise HTTPException(status_code=403, detail="Account is disabled")

    # Approval gate (women freelancers self-apply → pending until Super Admin
    # approves). They CAN sign in while pending/rejected, but the session carries
    # approvalStatus and the contributor portal shows a status-only screen
    # (status + logout, nothing else) until approved. Most roles are 'approved'
    # so this is transparent for them.
    approval = row.get("approval_status", "approved")

    # MFA branching
    if row.get("mfa_enabled"):
        pending = create_mfa_pending_token(_claims(row))
        return {
            "status": "mfa_required",
            "mfa_pending_token": pending,
            "expires_in": 600,
            "user": user_row_to_out(row).model_dump(include={"id", "email", "firstName", "lastName", "role"}),
        }

    repo.mark_login(str(row["id"]))
    publish_event("user.logged_in", {"userId": str(row["id"]), "email": row["email"], "role": row.get("role")})
    write_audit(actor_id=str(row["id"]), actor_email=row["email"], actor_role=row.get("role"),
                action="login", service="auth-service",
                ip_address=request.client.host if request.client else None)

    result = _token_pair(row)
    # Persist a session record so the sessions list is populated after login.
    try:
        from auth_app.routers.sessions import create_session  # local import avoids circularity
        create_session(
            account_id=str(row["id"]),
            refresh_token=result.get("refresh_token", ""),
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
    except Exception as _ses_exc:  # noqa: BLE001
        logger.warning("Failed to persist session record: %s", _ses_exc)

    if row.get("must_change_password"):
        result["user"]["requiresPasswordChange"] = True
    return result


@router.post("/validate")
async def validate(body: LoginRequest):
    """Credential-only validation used by the frontend before NextAuth sign-in."""
    row = repo.find_account_by_email(body.email)
    if not row:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not row.get("is_password_set") or not row.get("password_hash"):
        return {"status": "needs_password_setup", "user": {"id": str(row["id"]), "email": row["email"]}}
    if not body.password or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if row.get("mfa_enabled"):
        return {"status": "mfa_required", "mfa_pending_token": create_mfa_pending_token(_claims(row)),
                "expires_in": 600}
    return {"status": "ok", "user": user_row_to_out(row).model_dump()}


# ── Register ─────────────────────────────────────────────────────────────────

@router.get("/email-available")
async def email_available(email: str):
    """Public pre-signup check so the UI can warn about a duplicate email BEFORE
    asking the user to verify an OTP (otherwise the OTP is wasted on an email
    that can't register). Returns {available: bool}."""
    exists = bool(repo.find_account_by_email(email)) if email and "@" in email else False
    return {"available": not exists, "exists": exists}


@router.post("/register/contributor")
async def register_contributor(body: ContributorRegisterRequest, request: Request):
    if body.confirmPassword is not None and body.password != body.confirmPassword:
        raise HTTPException(status_code=422, detail="Passwords do not match")
    if repo.find_account_by_email(body.email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    # Women freelancers self-apply → held 'pending' until a Super Admin approves.
    # Everyone else under the contributor umbrella is auto-approved.
    is_women = (getattr(body, "segment", None) == "women") or bool(getattr(body, "requiresApproval", False))

    # Women applicants MUST verify their email via OTP before the application can
    # be created — no verified email, no account. (Redis remembers the verify for
    # 30 min; is_recently_verified fails closed if Redis is down.)
    if is_women and not await is_recently_verified("email", body.email):
        raise HTTPException(
            status_code=403,
            detail="Please verify your email with the code we sent before submitting your application.",
        )

    approval = "pending" if is_women else "approved"
    row = repo.create_account(
        email=body.email, password_hash=hash_password(body.password),
        first_name=body.firstName, last_name=body.lastName, role="contributor",
        phone=body.phone, provider="password", approval_status=approval,
    )
    # Women applicants are email-verified by definition (gated above).
    if is_women:
        try:
            repo.set_email_verified(body.email)
        except Exception:  # noqa: BLE001
            pass
        # Surface them in the Super Admin KYC Reviews queue: create a pending
        # contributor_kyc row carrying the application details. Approving it there
        # clears their approval gate (women/student must pass KYC before access).
        try:
            repo.create_pending_kyc(str(row["id"]), {
                "segment": getattr(body, "segment", "women") or "women",
                "fullName": f"{body.firstName} {body.lastName}".strip(),
                "org": getattr(body, "applicationOrg", None),
                "background": getattr(body, "applicationBackground", None),
                "docs": getattr(body, "applicationDocs", None) or [],
                "docUrl": getattr(body, "applicationDocUrl", None),
            })
        except Exception:  # noqa: BLE001
            pass
    repo.create_contributor_profile(str(row["id"]), body.model_dump())
    publish_event("user.registered", {"userId": str(row["id"]), "email": row["email"],
                                       "role": "contributor", "approvalStatus": approval})
    write_audit(actor_id=str(row["id"]), actor_email=row["email"], actor_role="contributor",
                action="register_contributor", service="auth-service",
                ip_address=request.client.host if request.client else None)
    return {"user": user_row_to_out(row).model_dump()}


@router.post("/register/mentor")
async def register_mentor(request: Request):
    """POST /api/v1/auth/register/mentor — self-register with invite code.

    Body: { firstName, lastName, email, password, inviteCode }
    Returns: { ok, userId, email }
    """
    from pydantic import BaseModel, EmailStr, field_validator
    import re as _re

    class _MentorRegisterBody(BaseModel):
        firstName: str = ""
        lastName: str = ""
        email: EmailStr
        password: str
        inviteCode: str

        @field_validator("firstName")
        @classmethod
        def _first_name_min(cls, v: str) -> str:
            v = v.strip()
            if len(v) < 2:
                raise ValueError("First name must be at least 2 characters")
            return v

        @field_validator("password")
        @classmethod
        def _password_strength(cls, v: str) -> str:
            if len(v) < 8 or not _re.search(r"[A-Z]", v) or not _re.search(r"[a-z]", v) or not _re.search(r"\d", v):
                raise ValueError("Password must include upper, lower, and a number.")
            return v

    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    try:
        body_m = _MentorRegisterBody.model_validate(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Check your details and try again.")

    from shared.db import get_pg_connection, ensure_pg_clean
    from psycopg2.extras import RealDictCursor

    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mentor_invite_codes (
                code        TEXT PRIMARY KEY,
                created_by  TEXT,
                used_by     TEXT,
                used_at     TIMESTAMPTZ,
                expires_at  TIMESTAMPTZ,
                is_active   BOOLEAN NOT NULL DEFAULT TRUE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()
        cur.execute(
            "SELECT * FROM mentor_invite_codes WHERE code = %s",
            (body_m.inviteCode,),
        )
        invite = cur.fetchone()

    if not invite:
        raise HTTPException(status_code=400, detail="Invalid or expired invite code")
    if not invite.get("is_active"):
        raise HTTPException(status_code=400, detail="This invite code has already been used")
    if invite.get("expires_at") and invite["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Invite code has expired")
    if invite.get("used_by"):
        raise HTTPException(status_code=409, detail="This invite code has already been used")

    if repo.find_account_by_email(body_m.email):
        raise HTTPException(status_code=409,
                            detail="An account with this email already exists")

    row = repo.create_account(
        email=body_m.email,
        password_hash=hash_password(body_m.password),
        first_name=body_m.firstName,
        last_name=body_m.lastName,
        role="mentor",
        provider="password",
        approval_status="approved",
    )

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE mentor_invite_codes SET is_active = FALSE, used_by = %s, "
            "used_at = now() WHERE code = %s",
            (str(row["id"]), body_m.inviteCode),
        )
    conn.commit()

    # Ensure a mentor_profiles row exists.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mentor_profiles (mentor_id, display_name)
            VALUES (%s, %s)
            ON CONFLICT (mentor_id) DO NOTHING
            """,
            (str(row["id"]), f"{body_m.firstName} {body_m.lastName}".strip()),
        )
    conn.commit()

    publish_event("user.registered", {
        "userId": str(row["id"]),
        "email": row["email"],
        "role": "mentor",
    })
    write_audit(
        actor_id=str(row["id"]),
        actor_email=row["email"],
        actor_role="mentor",
        action="register_mentor",
        service="auth-service",
        ip_address=request.client.host if request.client else None,
        extra={"inviteCode": body_m.inviteCode},
    )
    return {"ok": True, "userId": str(row["id"]), "email": row["email"]}


@router.post("/register/enterprise")
async def register_enterprise(body: EnterpriseRegisterRequest, request: Request):
    if repo.find_account_by_email(body.email):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    # Unique, human-readable company code (e.g. ACME-7K2); tenant_id derives
    # from it so it's stable + unique (the old hash() was non-deterministic).
    company_code = repo.generate_company_code(body.orgName)
    tenant_id = f"ent_{company_code.lower().replace('-', '_')}"
    row = repo.create_account(
        email=body.email, password_hash=hash_password(body.password),
        first_name=body.firstName, last_name=body.lastName, role="enterprise",
        phone=body.phone, provider="password", tenant_id=tenant_id,
    )
    profile_id = repo.create_enterprise_profile(str(row["id"]), body.model_dump(), tenant_id, company_code)
    publish_event("enterprise.registered", {"userId": str(row["id"]), "org": body.orgName,
                                             "tenantId": tenant_id, "companyCode": company_code})
    write_audit(actor_id=str(row["id"]), actor_email=row["email"], actor_role="enterprise",
                action="register_enterprise", target=body.orgName, service="auth-service",
                extra={"companyCode": company_code})
    return {"user": user_row_to_out(row).model_dump(), "enterprise_profile_id": profile_id,
            "companyCode": company_code, "tenantId": tenant_id}


# ── Token lifecycle ──────────────────────────────────────────────────────────

@router.post("/refresh")
async def refresh(body: RefreshRequest):
    try:
        payload = decode_token(body.refresh_token, expected_purpose="refresh")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    row = repo.find_account_by_id(payload.get("sub"))
    if not row:
        raise HTTPException(status_code=401, detail="Account not found")
    access = create_access_token(_claims(row))
    return {"access_token": access, "refresh_token": create_refresh_token(_claims(row)),
            "token_type": "bearer", "expires_in": access_token_seconds()}


@router.post("/logout")
async def logout(body: LogoutRequest):
    return {"ok": True}


@router.post("/logout-all")
async def logout_all(user: Annotated[dict, Depends(get_current_user)]):
    return {"ok": True}


@router.get("/me")
async def me(user: Annotated[dict, Depends(get_current_user)]):
    row = repo.find_account_by_id(user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return user_row_to_out(row).model_dump()


# ── MFA (TOTP) ───────────────────────────────────────────────────────────────

def _account_from_any_token(token: str) -> dict:
    """Accept either an mfa_pending or access token for MFA setup."""
    try:
        payload = decode_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")
    if payload.get("purpose") not in ("mfa_pending", "access"):
        raise HTTPException(status_code=401, detail="Wrong token type")
    row = repo.find_account_by_id(payload.get("sub"))
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")
    return row


@router.post("/mfa/setup/init")
async def mfa_setup_init(token: Annotated[str, Depends(get_bearer_token)]):
    row = _account_from_any_token(token)
    secret = row.get("mfa_secret") or pyotp.random_base32()
    if not row.get("mfa_secret"):
        repo.set_mfa_secret(str(row["id"]), secret)
    otpauth = pyotp.totp.TOTP(secret).provisioning_uri(name=row["email"], issuer_name="GlimmoraTeam")
    img = qrcode.make(otpauth)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    return {"secret": secret, "otpauth_uri": otpauth, "qr_code_png_base64": qr_b64}


@router.post("/mfa/setup/confirm")
async def mfa_setup_confirm(body: MfaCodeRequest, token: Annotated[str, Depends(get_bearer_token)]):
    row = _account_from_any_token(token)
    secret = row.get("mfa_secret")
    if not secret:
        raise HTTPException(status_code=409, detail={"code": "WRONG_MFA_PHASE", "message": "Run setup/init first"})
    if not pyotp.TOTP(secret).verify(body.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid code")
    recovery = [generate_temp_password(10) for _ in range(8)]
    repo.enable_mfa(str(row["id"]), recovery)
    repo.mark_login(str(row["id"]))
    pair = _token_pair(row | {"mfa_enabled": True})
    return {"recovery_codes": recovery, **pair}


@router.post("/mfa/verify")
async def mfa_verify(body: MfaCodeRequest, token: Annotated[str, Depends(get_bearer_token)], request: Request):
    try:
        payload = decode_token(token, expected_purpose="mfa_pending")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid MFA token")
    row = repo.find_account_by_id(payload.get("sub"))
    if not row or not row.get("mfa_secret"):
        raise HTTPException(status_code=400, detail="MFA not configured")
    if not pyotp.TOTP(row["mfa_secret"]).verify(body.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid code")
    repo.mark_login(str(row["id"]))
    result = _token_pair(row)
    try:
        from auth_app.routers.sessions import create_session
        create_session(
            account_id=str(row["id"]),
            refresh_token=result.get("refresh_token", ""),
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
    except Exception as _ses_exc:  # noqa: BLE001
        logger.warning("Failed to persist MFA session record: %s", _ses_exc)
    return result


@router.post("/mfa/recovery")
async def mfa_recovery(body: MfaRecoveryRequest, token: Annotated[str, Depends(get_bearer_token)]):
    try:
        payload = decode_token(token, expected_purpose="mfa_pending")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid MFA token")
    row = repo.find_account_by_id(payload.get("sub"))
    codes = (row or {}).get("mfa_recovery_codes") or []
    if not row or body.recovery_code not in codes:
        raise HTTPException(status_code=400, detail="Invalid recovery code")
    remaining = [c for c in codes if c != body.recovery_code]
    repo.enable_mfa(str(row["id"]), remaining)
    return _token_pair(row)


# ── Password flows ───────────────────────────────────────────────────────────

@router.post("/password/forgot")
async def password_forgot(body: ForgotPasswordRequest):
    row = repo.find_account_by_email(body.email)
    # Always 200 to avoid account enumeration.
    if row:
        token = create_reset_token({"sub": str(row["id"]), "email": row["email"]})
        link = f"{settings.frontend_base_url}/auth/reset-password?token={token}"
        send_email(to_email=row["email"], subject="Reset your Glimmora password",
                   body=f"Reset your password: {link}\n\nThis link expires in 30 minutes.", is_otp=True)
    return {"ok": True, "message": "If an account exists, a reset link has been sent."}


@router.post("/password/reset")
async def password_reset(body: ResetPasswordRequest):
    if body.confirmPassword is not None and body.new_password != body.confirmPassword:
        raise HTTPException(status_code=422, detail="Passwords do not match")
    try:
        payload = decode_token(body.token, expected_purpose="reset")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    repo.set_password(payload["sub"], hash_password(body.new_password))
    write_audit(actor_id=payload["sub"], actor_email=payload.get("email"), actor_role=None,
                action="password_reset", service="auth-service")
    return {"ok": True}


@router.post("/password/change")
async def password_change(body: ChangePasswordRequest, user: Annotated[dict, Depends(get_current_user)]):
    if body.confirmPassword is not None and body.new_password != body.confirmPassword:
        raise HTTPException(status_code=422, detail="Passwords do not match")
    row = repo.find_account_by_id(user["id"])
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")
    # If a password is already set, require the old one.
    if row.get("is_password_set") and row.get("password_hash") and body.old_password is not None:
        if not verify_password(body.old_password, row["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
    repo.set_password(user["id"], hash_password(body.new_password))
    return {"ok": True}


# ── OTP (email + phone) ──────────────────────────────────────────────────────

# ── Public application-document upload (pre-account, e.g. Women in Tech) ──────

# Applicants upload supporting files (images/PDF) BEFORE an account exists, so
# this endpoint is intentionally unauthenticated. It only accepts a small set of
# content types and a size cap, and files land under a dedicated `applications/`
# prefix in Vercel Blob. Returns descriptors the signup form attaches to the app.

_APP_DOC_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
_APP_DOC_ALLOWED = {
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif",
    "application/pdf",
}


@router.post("/application-upload")
async def application_upload(file: UploadFile = File(...)):
    if not blob_is_configured():
        raise HTTPException(status_code=503, detail="File storage is not configured.")
    content_type = (file.content_type or "").lower()
    if content_type not in _APP_DOC_ALLOWED:
        raise HTTPException(status_code=415, detail="Only images (PNG/JPG/WEBP/GIF) and PDF files are allowed.")
    data = await file.read()
    if len(data) > _APP_DOC_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Each file must be 10 MB or smaller.")
    if not data:
        raise HTTPException(status_code=422, detail="Empty file.")
    filename = (file.filename or "document").replace("/", "_").replace("\\", "_")
    try:
        descriptor = await upload_blob(
            pathname=f"applications/{filename}",
            data=data, content_type=content_type,
        )
    except RuntimeError:
        raise HTTPException(status_code=503, detail="File storage is not configured.")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Upload failed: {exc}")
    url = descriptor.get("url")
    if not url:
        raise HTTPException(status_code=502, detail="Upload returned no URL.")
    return {"url": url, "filename": filename, "contentType": content_type, "size": len(data)}


@router.post("/otp/send-email")
async def otp_send_email(body: OtpSendRequest):
    if not body.email:
        raise HTTPException(status_code=422, detail="Email required")
    code = generate_code()
    persisted = await store_otp("email", body.email, code)
    sent = send_email(to_email=body.email, subject="Your Glimmora verification code",
                      body=f"Your code is {code}. It expires in 5 minutes.", is_otp=True)
    resp = {"ok": True, "message": "Code sent."}
    import os
    if not persisted or not sent:
        resp.update({"devFallback": True, "devOtp": code,
                     "message": "Email/Redis unavailable — use the dev code."})
    elif os.getenv("OTP_DEV_ECHO") == "1":
        # Dev convenience (local, no Redis/SMTP inbox handy): echo the code.
        resp["devOtp"] = code
    return resp


@router.post("/otp/verify-email")
async def otp_verify_email(body: OtpVerifyRequest):
    if not body.email:
        raise HTTPException(status_code=422, detail="Email required")
    ok = await verify_otp("email", body.email, body.code)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    # Persist email_verified so the dashboard gate clears.
    row = repo.find_account_by_email(body.email)
    if row:
        repo.set_email_verified(body.email)
    return {"ok": True, "verified": True, "accountExists": bool(row)}


@router.post("/otp/send-phone")
async def otp_send_phone(body: OtpSendRequest):
    if not body.phone:
        raise HTTPException(status_code=422, detail="Phone required")
    code = generate_code()
    persisted = await store_otp("phone", body.phone, code)
    # No SMS provider configured → dev fallback (returns code).
    return {"ok": True, "devFallback": True, "devOtp": code,
            "message": "SMS provider not configured — use the dev code shown."}


@router.post("/otp/verify-phone")
async def otp_verify_phone(body: OtpVerifyRequest):
    if not body.phone:
        raise HTTPException(status_code=422, detail="Phone required")
    ok = await verify_otp("phone", body.phone, body.code)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    return {"ok": True, "verified": True}


# ── OTP-verified password setup (completes the hybrid OAuth→password flow) ────

class _SetupAfterOtp(OtpVerifyRequest):
    new_password: str


@router.post("/password/setup-after-otp")
async def setup_after_otp(body: _SetupAfterOtp):
    if not body.email:
        raise HTTPException(status_code=422, detail="Email required")
    if not await verify_otp("email", body.email, body.code):
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    row = repo.find_account_by_email(body.email)
    if not row:
        raise HTTPException(status_code=404, detail="Account not found")
    repo.set_password(str(row["id"]), hash_password(body.new_password))
    refreshed = repo.find_account_by_id(str(row["id"]))
    return _token_pair(refreshed)
