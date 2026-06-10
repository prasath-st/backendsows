"""
User management — /api/superadmin/users and /api/v1/users**.

Creates login_accounts rows (with a generated temp password when none given),
optionally emails credentials via shared.mailer, audits the action, and supports
reviewer invitation / resend flows the frontend reviewer pages proxy to.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from shared.audit import write_audit
from shared.config import settings
from shared.deps import get_current_admin, get_current_superadmin
from shared.kafka_bus import publish_event
from shared.mailer import build_credentials_body, send_email
from shared.security import generate_temp_password, hash_password

from superadmin_app import repo

router = APIRouter(tags=["superadmin-users"])


class CreateUserRequest(BaseModel):
    """Flexible create-user payload — only email + role are essential."""
    email: str
    firstName: str | None = None
    lastName: str | None = None
    name: str | None = None
    role: str | None = "member"
    phone: str | None = None
    department: str | None = None
    tenantId: str | None = None
    password: str | None = None
    # Either of these triggers a credentials email.
    sendInvitation: bool | None = None
    sendCredentials: bool | None = None
    # Used by the /api/v1/users reviewer-invite proxy: if the email already
    # exists, behave like a resend rather than 409.
    resendExisting: bool | None = None
    # Inviter-based tenant scoping: the enterprise proxy passes the inviting
    # admin's email so the new member inherits that admin's tenant_id (and thus
    # only appears in that enterprise's tenant registry).
    callerEmail: str | None = None


def _split_name(body: CreateUserRequest) -> tuple[str, str]:
    first = (body.firstName or "").strip()
    last = (body.lastName or "").strip()
    if not first and body.name:
        parts = body.name.strip().split(" ", 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else ""
    return first, last


def _wants_credentials(body: CreateUserRequest) -> bool:
    return bool(body.sendCredentials) or bool(body.sendInvitation)


def _login_url(role: str | None) -> str:
    """Per-portal login URL for credential emails (the global /auth/login was
    removed in the per-portal auth redesign). Maps the account's role to its
    portal login so the email tells the user exactly where to sign in."""
    base = settings.frontend_base_url.rstrip("/")
    r = (role or "").lower()
    scope = r.split(".")[0] if "." in r else r
    if scope == "mentor":
        portal = "mentor"
    elif scope == "reviewer":
        portal = "reviewer"
    elif scope in ("enterprise", "ent"):
        portal = "enterprise"
    elif scope in ("admin", "superadmin", "super_admin", "plat", "university_admin"):
        portal = "admin"
    else:
        portal = "contributor"  # contributor / student / freelancer / women
    return f"{base}/{portal}/login"


def _email_credentials(*, name: str, email: str, temp_password: str, role: str | None) -> bool:
    text, html = build_credentials_body(
        name=name or email, email=email, temp_password=temp_password,
        login_url=_login_url(role), org_name="Glimmora",
    )
    return send_email(
        to_email=email,
        subject="Your Glimmora account credentials",
        body=text, html=html,
    )


def _resend_invite(existing: dict[str, Any], actor: dict, request: Request,
                   tenant_id: str | None = None) -> dict[str, Any]:
    """Regenerate a temp password for an existing account and email it. If a
    tenantId is supplied and the account isn't linked yet, backfill it so the
    tenant's provisioning timeline picks the account up."""
    if tenant_id and not existing.get("tenant_id"):
        repo.link_tenant(str(existing["id"]), tenant_id)
        existing = {**existing, "tenant_id": tenant_id}
    temp = generate_temp_password()
    repo.set_temp_password(str(existing["id"]), hash_password(temp))
    name = existing.get("name") or f"{existing.get('first_name','')} {existing.get('last_name','')}".strip()
    sent = _email_credentials(name=name, email=existing["email"],
                              temp_password=temp, role=existing.get("role"))
    write_audit(actor_id=actor.get("id"), actor_email=actor.get("email"), actor_role=actor.get("role"),
                action="resend_invite", target=existing["email"], target_id=str(existing["id"]),
                service="superadmin-service",
                ip_address=request.client.host if request.client else None,
                extra={"emailSent": sent})
    refreshed = repo.find_account_by_id(str(existing["id"]))
    out = repo.user_out(refreshed or existing)
    out["emailSent"] = sent
    out["resent"] = True
    # Surface the fresh temp password when we couldn't email it, so the admin can
    # copy it from the UI (cloud deploys have no SMTP).
    if not sent:
        out["tempPassword"] = temp
    return out


def _create_user(body: CreateUserRequest, actor: dict, request: Request) -> dict[str, Any]:
    if not body.email or "@" not in body.email:
        raise HTTPException(status_code=422, detail="A valid email is required")

    requested_role = (body.role or "member").lower()
    existing = repo.find_account_by_email(body.email)
    if existing:
        existing_role = (existing.get("role") or "").lower()
        # One email = one role FAMILY. Compare the scope (part before the dot) so
        # tier variants of the same role — mentor / mentor.senior / mentor.lead,
        # or ent.admin / ent.reviewer — are NOT treated as a conflict; only a
        # genuinely different family (e.g. mentor vs enterprise) is blocked.
        def _scope(r: str) -> str:
            return r.split(".")[0] if "." in r else r
        if existing_role and _scope(existing_role) != _scope(requested_role):
            raise HTTPException(
                status_code=409,
                detail=(f"This email already has a {existing_role} account. "
                        f"Use a different email, or manage the existing {existing_role} user."),
            )
        # Same-role re-provision: resend a fresh temp password (and link the
        # tenant if needed). The UI's "Resend credentials" button and the
        # inline resend both land here. resendExisting is kept for older callers.
        if body.resendExisting or _wants_credentials(body):
            return _resend_invite(existing, actor, request, tenant_id=body.tenantId)
        raise HTTPException(
            status_code=409,
            detail=f"This email already has a {existing_role or 'platform'} account — use Resend credentials to email a fresh temporary password.",
        )

    first, last = _split_name(body)
    role = (body.role or "member").lower()

    # Inheriting the inviter's tenant: if no explicit tenantId was given but a
    # callerEmail was, stamp the new account with the inviting admin's tenant_id
    # so it shows up only in that enterprise's member registry.
    tenant_id = body.tenantId
    if not tenant_id and body.callerEmail:
        caller = repo.find_account_by_email(body.callerEmail)
        if caller and caller.get("tenant_id"):
            tenant_id = caller["tenant_id"]

    temp_password = body.password or generate_temp_password()
    password_hash = hash_password(temp_password)
    # If no explicit password was supplied, the user must change it on first login.
    must_change = body.password is None

    row = repo.create_account(
        email=body.email, password_hash=password_hash,
        first_name=first, last_name=last, role=role,
        phone=body.phone, department=body.department, tenant_id=tenant_id,
        must_change_password=must_change,
    )

    email_sent = False
    if _wants_credentials(body):
        email_sent = _email_credentials(
            name=row.get("name") or body.email, email=row["email"],
            temp_password=temp_password, role=role,
        )

    publish_event("user.created", {"userId": str(row["id"]), "email": row["email"], "role": role})
    write_audit(actor_id=actor.get("id"), actor_email=actor.get("email"), actor_role=actor.get("role"),
                action="create_user", target=row["email"], target_id=str(row["id"]),
                service="superadmin-service",
                ip_address=request.client.host if request.client else None,
                extra={"role": role, "emailSent": email_sent})

    out = repo.user_out(row)
    out["emailSent"] = email_sent
    # Surface the temp password only when we are NOT emailing it, so the admin can copy it.
    if not email_sent and body.password is None:
        out["tempPassword"] = temp_password
    return out


# ── POST /api/superadmin/users ────────────────────────────────────────────────

@router.get("/api/superadmin/users/check-email")
async def check_email(
    email: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Inline duplicate check — does an account already exist for this email
    (across ALL roles, since login_accounts is one table)? Powers real-time
    'already provisioned' validation as the admin enters an email."""
    row = repo.find_account_by_email(email.strip()) if email and "@" in email else None
    return {"exists": bool(row), "role": row.get("role") if row else None}


@router.post("/api/superadmin/users", status_code=201)
async def superadmin_create_user(
    body: CreateUserRequest,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    return _create_user(body, admin, request)


@router.delete("/api/superadmin/users/{user_id}")
async def superadmin_delete_user(
    user_id: str,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Hard-delete a provisioned account (mentor / reviewer / contributor / etc.).
    Also clears any sow_mentor / sow_reviewer assignment records pointing at it."""
    from shared.db import get_pg_connection
    existing = repo.find_account_by_id(user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Account not found")
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM login_accounts WHERE id = %s", (user_id,))
        # Best-effort: drop assignment records that reference this account's email.
        email = existing.get("email")
        if email:
            cur.execute(
                "DELETE FROM admin_records WHERE kind IN ('sow_mentor','sow_reviewer') "
                "AND data::text ILIKE %s",
                (f"%{email}%",),
            )
    conn.commit()
    write_audit(actor_id=admin.get("id"), actor_email=admin.get("email"), actor_role=admin.get("role"),
                action="delete_user", target=existing.get("email"), target_id=str(user_id),
                service="superadmin-service",
                ip_address=request.client.host if request.client else None,
                extra={"role": existing.get("role")})
    return {"ok": True, "deletedId": str(user_id)}


# ── Applications approval queue (women freelancers self-apply) ────────────────

@router.get("/api/superadmin/applications")
async def list_applications(
    admin: Annotated[dict, Depends(get_current_admin)],
    status: str = "pending",
):
    return {"applications": [repo.user_out(r) for r in repo.list_applications(status)]}


class ApprovalDecision(BaseModel):
    decision: str  # 'approve' | 'reject'


@router.post("/api/superadmin/applications/{account_id}/decision")
async def decide_application(
    account_id: str,
    body: ApprovalDecision,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    new_status = "approved" if body.decision == "approve" else "rejected"
    row = repo.set_approval_status(account_id, new_status)
    if not row:
        raise HTTPException(status_code=404, detail="Application not found")
    # Notify the applicant by email, including their portal login URL.
    login_url = f"{settings.frontend_base_url}/contributor/login"
    if new_status == "approved":
        send_email(
            to_email=row["email"],
            subject="Your Glimmora application is approved",
            body=(f"Good news! Your application has been approved.\n\n"
                  f"Sign in here: {login_url}\n\nComplete your profile to access your dashboard."),
            category="application_approved",
        )
    else:
        send_email(
            to_email=row["email"],
            subject="Update on your Glimmora application",
            body="Thank you for applying. Unfortunately your application was not approved at this time.",
            category="application_rejected",
        )
    write_audit(actor_id=str(admin.get("id", "")), actor_email=admin.get("email", ""),
                actor_role=admin.get("role", "superadmin"), action=f"application_{new_status}",
                target="login_account", target_id=str(account_id),
                details=f"Application {new_status} for {row['email']}", service="superadmin-service",
                ip_address=request.client.host if request.client else None)
    return {"ok": True, "account_id": account_id, "approval_status": new_status}


# ── POST /api/v1/users (reviewer/create + reviewers/invitations proxy) ────────

@router.post("/api/v1/users", status_code=201)
async def v1_create_user(
    body: CreateUserRequest,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    return _create_user(body, admin, request)


# ── POST /api/v1/users/reviewers/resend-invite ────────────────────────────────

class ResendInviteRequest(BaseModel):
    email: str | None = None
    userId: str | None = None


@router.post("/api/v1/users/reviewers/resend-invite")
async def reviewer_resend_invite(
    body: ResendInviteRequest,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    existing = None
    if body.userId:
        existing = repo.find_account_by_id(body.userId)
    if not existing and body.email:
        existing = repo.find_account_by_email(body.email)
    if not existing:
        raise HTTPException(status_code=404, detail="Reviewer account not found")
    return _resend_invite(existing, admin, request)


# ── GET /api/v1/users/reviewers/list ──────────────────────────────────────────

@router.get("/api/v1/users/reviewers/list")
async def reviewer_list(admin: Annotated[dict, Depends(get_current_admin)]):
    reviewers = repo.list_reviewers()
    return {"reviewers": reviewers, "total": len(reviewers)}
