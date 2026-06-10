"""Audit log + platform notifications (educore-pattern CRUD).

Endpoints (Super Admin):
  GET    /api/superadmin/audit-log              — paginated, filterable
  GET    /api/superadmin/audit-log/export       — CSV export
  DELETE /api/superadmin/audit-log/{entry_id}   — delete one entry
  DELETE /api/superadmin/audit-log/bulk         — clear by filter
  GET    /api/superadmin/notifications          — list
  POST   /api/superadmin/notifications/{id}/dismiss
  POST   /api/superadmin/notifications/dismiss-all

Reads use shared.audit.query_audit (Mongo `audit_log`). Notifications are
derived from recent audit events so the surface always has live data even
before a dedicated notifications store exists.
"""

from __future__ import annotations

import csv
import io
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from shared.audit import query_audit, write_audit
from shared.db import mongo_audit_collection, get_pg_connection
from shared.deps import get_current_admin

router = APIRouter(tags=["audit"])


@router.get("/api/superadmin/dashboard")
async def platform_dashboard(admin: Annotated[dict, Depends(get_current_admin)]):
    """Platform-wide stats for the admin dashboard: account counts by role,
    pending applications, and recent audit activity."""
    counts: dict[str, int] = {}
    pending = 0
    total = 0
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT role, COUNT(*) FROM login_accounts GROUP BY role")
            for role, n in cur.fetchall():
                counts[role or "unknown"] = int(n)
                total += int(n)
            cur.execute("SELECT COUNT(*) FROM login_accounts WHERE approval_status = 'pending'")
            pending = int(cur.fetchone()[0])
    except Exception:  # noqa: BLE001
        pass
    recent = query_audit(filters={}, page=1, page_size=8).get("items", [])
    return {
        "stats": {
            "total_accounts": total,
            "by_role": counts,
            "pending_applications": pending,
            "contributors": counts.get("contributor", 0),
            "enterprises": counts.get("enterprise", 0),
            "mentors": counts.get("mentor", 0),
        },
        "recent_activity": [
            {
                "id": r.get("_id"),
                "action": r.get("action"),
                "actor": r.get("actorEmail") or "system",
                "service": r.get("service"),
                "timestamp": r.get("timestamp"),
            }
            for r in recent
        ],
    }


def _oid(entry_id: str):
    """Best-effort ObjectId parse (Mongo _id); fall back to raw string match."""
    try:
        from bson import ObjectId
        return ObjectId(entry_id)
    except Exception:
        return entry_id


@router.get("/api/superadmin/audit-log")
async def list_audit_log(
    admin: Annotated[dict, Depends(get_current_admin)],
    page: int = 1,
    page_size: int = 50,
    actor: str | None = None,
    action: str | None = None,
    service: str | None = None,
):
    filters: dict[str, Any] = {}
    if actor:
        filters["actorEmail"] = actor
    if action:
        filters["action"] = action
    if service:
        filters["service"] = service
    return query_audit(filters=filters, page=page, page_size=page_size)


@router.get("/api/superadmin/audit-log/export")
async def export_audit_log(
    admin: Annotated[dict, Depends(get_current_admin)],
    actor: str | None = None,
    action: str | None = None,
):
    filters: dict[str, Any] = {}
    if actor:
        filters["actorEmail"] = actor
    if action:
        filters["action"] = action
    data = query_audit(filters=filters, page=1, page_size=200)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["timestamp", "actorEmail", "actorRole", "action", "target", "targetId", "service", "details", "ipAddress"])
    for r in data.get("items", []):
        writer.writerow([
            r.get("timestamp", ""), r.get("actorEmail", ""), r.get("actorRole", ""),
            r.get("action", ""), r.get("target", ""), r.get("targetId", ""),
            r.get("service", ""), r.get("details", ""), r.get("ipAddress", ""),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-log.csv"},
    )


@router.delete("/api/superadmin/audit-log/bulk")
async def clear_audit_log(
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
    action: str | None = None,
):
    col = mongo_audit_collection()
    if col is None:
        raise HTTPException(status_code=503, detail="Audit store unavailable")
    q: dict[str, Any] = {}
    if action:
        q["action"] = action
    res = col.delete_many(q)
    write_audit(actor_id=str(admin.get("id", "")), actor_email=admin.get("email"),
                actor_role=admin.get("role"), action="audit_log_bulk_delete",
                service="superadmin-service", details=f"deleted {res.deleted_count} entries",
                ip_address=request.client.host if request.client else None)
    return {"ok": True, "deleted": res.deleted_count}


@router.delete("/api/superadmin/audit-log/{entry_id}")
async def delete_audit_entry(
    entry_id: str,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    col = mongo_audit_collection()
    if col is None:
        raise HTTPException(status_code=503, detail="Audit store unavailable")
    res = col.delete_one({"_id": _oid(entry_id)})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Audit entry not found")
    return {"ok": True, "deleted_id": entry_id}


# ── Notifications (derived from recent audit events) ──────────────────────────

@router.get("/api/superadmin/notifications")
async def list_notifications(
    admin: Annotated[dict, Depends(get_current_admin)],
    page: int = 1,
    page_size: int = 20,
):
    data = query_audit(filters={}, page=page, page_size=page_size)
    items = [
        {
            "id": r.get("_id"),
            "title": r.get("action", "Event"),
            "message": r.get("details") or f"{r.get('actorEmail','system')} · {r.get('action','')}",
            "timestamp": r.get("timestamp"),
            "read": False,
        }
        for r in data.get("items", [])
    ]
    return {"notifications": items, "total": data.get("total", 0), "page": page}


@router.post("/api/superadmin/notifications/{notif_id}/dismiss")
async def dismiss_notification(notif_id: str, admin: Annotated[dict, Depends(get_current_admin)]):
    # Notifications are a read-only projection of audit events; dismiss is a
    # client-side acknowledgement. Return ok so the UI can update.
    return {"ok": True, "id": notif_id}


@router.post("/api/superadmin/notifications/dismiss-all")
async def dismiss_all_notifications(admin: Annotated[dict, Depends(get_current_admin)]):
    return {"ok": True}


# ── Generic admin records CRUD (tenants/mentors/skill-taxonomy/kyc/etc.) ──────
# One JSONB table per `kind`; full CRUD + audit + soft-delete (educore pattern).

import uuid as _uuid
from psycopg2.extras import Json, RealDictCursor


def _rec_out(row: dict) -> dict:
    data = row.get("data") or {}
    return {
        "id": row["id"], "kind": row["kind"], "name": row.get("name"),
        "status": row.get("status"),
        "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        **(data if isinstance(data, dict) else {}),
    }


@router.get("/api/superadmin/tenants/{tenant_id}/provisioning-status")
async def tenant_provisioning_status(
    tenant_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    admin_email: str | None = None,
):
    """Real provisioning status for a tenant, derived from live data:
      - scope/policies: done once the tenant has any account
      - admin invited: an enterprise admin account exists for the tenant
      - first sign-in: that admin has logged in AND set a real password
      - employees: more than just the admin account exists
      - first SOW: the tenant owns at least one SOW
    Returns step states the timeline renders directly (no mock).

    The tenant_id used in the UI (store/mock id) may not match the account's
    tenant_id for legacy accounts created before the link existed. When the
    tenant_id match yields nothing and the caller passes the known admin email,
    we fall back to resolving the admin by email so the timeline still tracks."""
    conn = get_pg_connection()
    admin_row = None
    employee_count = 0
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, email, role, last_login_at, must_change_password FROM login_accounts "
            "WHERE tenant_id = %s ORDER BY created_at ASC", (tenant_id,))
        rows = cur.fetchall()
        # Fallback: resolve the primary admin by email when the tenant link is missing.
        if not rows and admin_email and "@" in admin_email:
            cur.execute(
                "SELECT id, email, role, last_login_at, must_change_password FROM login_accounts "
                "WHERE LOWER(email) = LOWER(%s)", (admin_email,))
            rows = cur.fetchall()
        employee_count = len(rows)
        admin_row = next((r for r in rows if r["role"] == "enterprise"), rows[0] if rows else None)

    # SOW count for the tenant (enterprise_sows owned by the resolved admin).
    sow_count = 0
    if admin_row:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM enterprise_sows WHERE owner_id = %s",
                    (str(admin_row["id"]),))
                sow_count = int(cur.fetchone()[0])
        except Exception:  # noqa: BLE001 — table may live in another DB/service
            sow_count = 0

    admin_exists = admin_row is not None
    admin_signed_in = bool(admin_row and admin_row.get("last_login_at"))
    password_set = bool(admin_row and not admin_row.get("must_change_password"))

    # First admin sign-in is "done" only once they've logged in AND reset the
    # temporary password. Re-sending credentials reissues a temp password, so
    # this correctly flips back to pending until the next reset.
    signin_done = admin_signed_in and password_set

    # Downstream steps (employee import, SOW upload) happen INSIDE the enterprise
    # portal, which requires the admin to have signed in. So they can't be "done"
    # before sign-in — gate them on signin_done to keep the timeline monotonic
    # (no later step completing while an earlier prerequisite is still pending).
    employees_done = signin_done and employee_count > 1
    sow_done = signin_done and sow_count > 0

    def step(sid: str, label: str, done: bool) -> dict:
        return {"id": sid, "label": label, "state": "done" if done else "pending"}

    steps = [
        step("scope", "Tenant scope created", admin_exists),
        step("policies", "Default policies applied", admin_exists),
        step("admin", "Admin credentials sent", admin_exists),
        step("signin", "First admin sign-in", signin_done),
        step("employees", "Employees imported", employees_done),
        step("sow", "First SOW upload", sow_done),
    ]
    done = sum(1 for s in steps if s["state"] == "done")
    return {
        "tenantId": tenant_id,
        "steps": steps,
        "completePct": round(done / len(steps) * 100),
        "signals": {"adminExists": admin_exists, "adminSignedIn": admin_signed_in,
                    "passwordSet": password_set, "employeeCount": employee_count, "sowCount": sow_count},
    }


@router.get("/api/superadmin/sows")
async def list_all_sows(
    admin: Annotated[dict, Depends(get_current_admin)],
    tenant_id: str | None = None,
    status: str | None = None,
):
    """Platform-wide SOW visibility for the Super Admin. Every SOW any enterprise
    uploads (enterprise_sows) is listed here with its owner + tenant, so the
    platform sees them — not just the enterprise that created them."""
    conn = get_pg_connection()
    rows: list[dict] = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT s.id, s.owner_id, s.owner_email, s.source, s.data,
                   s.created_at, s.updated_at,
                   a.email AS account_email, a.tenant_id, a.first_name, a.last_name
              FROM enterprise_sows s
              LEFT JOIN login_accounts a ON a.id::text = s.owner_id
             ORDER BY s.created_at DESC
            """,
        )
        for r in cur.fetchall():
            data = r.get("data") or {}
            if isinstance(data, str):
                import json as _json
                try:
                    data = _json.loads(data)
                except (ValueError, TypeError):
                    data = {}
            stages = data.get("approvalStages") or []
            # The "current" stage is the first pending one (the gate keys off this).
            current_stage = next((s.get("key") for s in stages if s.get("status") == "pending"), None)
            item = {
                "id": r["id"],
                "ownerId": r["owner_id"],
                "ownerEmail": r.get("owner_email") or r.get("account_email"),
                "tenantId": r.get("tenant_id"),
                "source": r.get("source"),
                "projectTitle": data.get("projectTitle"),
                "clientOrganisation": data.get("clientOrganisation"),
                "status": data.get("status"),
                "aiApproved": data.get("aiApproved", False),
                "fileName": data.get("fileName"),
                "fileUrl": data.get("fileUrl"),
                "budgetAmount": data.get("budgetAmount"),
                "budgetCurrency": data.get("budgetCurrency"),
                "startDate": data.get("startDate"),
                "endDate": data.get("endDate"),
                "deadline": data.get("deadline"),
                "durationDays": data.get("durationDays"),
                "approvalStages": stages,
                "currentStage": current_stage,
                "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
                "updatedAt": r["updated_at"].isoformat() if r.get("updated_at") else None,
            }
            if tenant_id and item["tenantId"] != tenant_id:
                continue
            if status and item["status"] != status:
                continue
            rows.append(item)
    return {"items": rows, "total": len(rows)}


# ── Mentor assignment to a SOW (Super Admin, at the Commercial gate) ──────────

@router.get("/api/superadmin/mentors")
async def list_mentors(admin: Annotated[dict, Depends(get_current_admin)]):
    """Mentor accounts available for assignment (any role in the mentor family)."""
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, email, first_name, last_name, role FROM login_accounts "
            "WHERE role LIKE 'mentor%' AND COALESCE(is_active, TRUE) ORDER BY created_at DESC")
        mentors = [{
            "id": str(r["id"]), "email": r["email"],
            "name": (f"{r.get('first_name','') or ''} {r.get('last_name','') or ''}".strip() or r["email"]),
            "role": r["role"],
        } for r in cur.fetchall()]
    return {"mentors": mentors, "total": len(mentors)}


@router.get("/api/superadmin/contributors")
async def list_contributors(admin: Annotated[dict, Depends(get_current_admin)]):
    """Contributor accounts available for task assignment (contributor / freelancer
    / women / student). Used to populate the project task assign drawer with REAL
    people instead of mock candidates."""
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, email, first_name, last_name, role FROM login_accounts "
            "WHERE (role LIKE 'contributor%' OR role IN ('freelancer','women','student')) "
            "AND COALESCE(is_active, TRUE) ORDER BY created_at DESC")
        contributors = [{
            "id": str(r["id"]), "email": r["email"],
            "name": (f"{r.get('first_name','') or ''} {r.get('last_name','') or ''}".strip() or r["email"]),
            "role": r["role"],
        } for r in cur.fetchall()]
    return {"contributors": contributors, "total": len(contributors)}


@router.get("/api/superadmin/reviewers")
async def list_reviewers(admin: Annotated[dict, Depends(get_current_admin)]):
    """Reviewer accounts available for assignment to a SOW at intake (role in the
    reviewer family). Used to populate the SOW intake reviewer picker with REAL
    people instead of mock candidates."""
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, email, first_name, last_name, role FROM login_accounts "
            "WHERE role LIKE 'reviewer%' AND COALESCE(is_active, TRUE) ORDER BY created_at DESC")
        reviewers = [{
            "id": str(r["id"]), "email": r["email"],
            "name": (f"{r.get('first_name','') or ''} {r.get('last_name','') or ''}".strip() or r["email"]),
            "role": r["role"],
        } for r in cur.fetchall()]
    return {"reviewers": reviewers, "total": len(reviewers)}


@router.get("/api/superadmin/all-users")
async def list_all_users(
    admin: Annotated[dict, Depends(get_current_admin)],
    tenant_id: str | None = None,
    caller_email: str | None = None,
):
    """Provisioned accounts (for the enterprise member registry). status:
    'invited' = never signed in / must change password; 'active' = has signed in;
    'suspended' = is_active false.

    TENANT SCOPING (inviter-based): when the enterprise proxy passes the caller's
    email (or tenant_id), the list is filtered to that tenant only, so each
    enterprise workspace sees ONLY its own members — not every account on the
    platform. Super admins (no tenant filter) still get everything.
    """
    conn = get_pg_connection()
    # Resolve the tenant to scope to. caller_email takes priority — we look up the
    # caller's own account and use ITS tenant_id (can't be spoofed by the client).
    scope_tenant = tenant_id
    if caller_email:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT tenant_id FROM login_accounts WHERE lower(email)=lower(%s) LIMIT 1",
                        (caller_email,))
            row = cur.fetchone()
            if row:
                scope_tenant = row.get("tenant_id")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if scope_tenant:
            cur.execute(
                "SELECT id, email, first_name, last_name, role, tenant_id, is_active, "
                "must_change_password, last_login_at, created_at "
                "FROM login_accounts WHERE tenant_id = %s ORDER BY created_at DESC",
                (scope_tenant,))
        else:
            cur.execute(
                "SELECT id, email, first_name, last_name, role, tenant_id, is_active, "
                "must_change_password, last_login_at, created_at "
                "FROM login_accounts ORDER BY created_at DESC")
        users = []
        for r in cur.fetchall():
            if r.get("is_active") is False:
                status = "suspended"
            elif r.get("last_login_at"):
                status = "active"
            elif r.get("must_change_password"):
                status = "invited"
            else:
                status = "active"
            users.append({
                "id": str(r["id"]), "email": r["email"],
                "name": (f"{r.get('first_name','') or ''} {r.get('last_name','') or ''}".strip() or r["email"]),
                "role": r["role"], "status": status, "tenantId": r.get("tenant_id"),
                "lastLoginAt": r["last_login_at"].isoformat() if r.get("last_login_at") else None,
                "createdAt": r["created_at"].isoformat() if r.get("created_at") else None,
            })
    return {"users": users, "total": len(users), "tenantScope": scope_tenant}


@router.get("/api/superadmin/sows/{sow_id}/mentor")
async def get_sow_mentor(sow_id: str, admin: Annotated[dict, Depends(get_current_admin)]):
    """Currently-assigned mentor for a SOW (if any)."""
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT data FROM admin_records WHERE kind='sow_mentor' AND name=%s AND deleted_at IS NULL "
            "ORDER BY updated_at DESC LIMIT 1", (sow_id,))
        row = cur.fetchone()
    data = (row or {}).get("data") or {}
    if isinstance(data, str):
        try:
            data = _json_loads(data)
        except Exception:  # noqa: BLE001
            data = {}
    return {"sowId": sow_id, "mentor": data or None}


class AssignMentor(BaseModel):
    mentorId: str
    mentorEmail: str | None = None
    mentorName: str | None = None


@router.post("/api/superadmin/sows/{sow_id}/assign-mentor")
async def assign_sow_mentor(sow_id: str, body: AssignMentor, request: Request,
                            admin: Annotated[dict, Depends(get_current_admin)]):
    """Assign (or re-assign) the QA mentor for a SOW. Stored in admin_records
    (kind=sow_mentor, name=sow_id). Contributor submissions for this SOW then
    route to this mentor instead of the open pool."""
    conn = get_pg_connection()
    rid = f"sow_mentor_{sow_id}"
    payload = {"mentorId": body.mentorId, "mentorEmail": body.mentorEmail, "mentorName": body.mentorName}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_records (id, kind, name, status, data)
            VALUES (%s, 'sow_mentor', %s, 'assigned', %s)
            ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, status='assigned',
                                           deleted_at = NULL, updated_at = now()
            """,
            (rid, sow_id, Json(payload)))
    conn.commit()
    write_audit(actor_id=str(admin.get("id", "")), actor_email=admin.get("email"),
                actor_role=admin.get("role"), action="assign_mentor", target="sow",
                target_id=sow_id, service="superadmin-service",
                ip_address=request.client.host if request.client else None,
                extra={"mentorId": body.mentorId})
    return {"ok": True, "sowId": sow_id, "mentor": payload}


@router.get("/api/superadmin/sows/{sow_id}/reviewer")
async def get_sow_reviewer(sow_id: str, admin: Annotated[dict, Depends(get_current_admin)]):
    """Currently-assigned reviewer for a SOW (if any)."""
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT data FROM admin_records WHERE kind='sow_reviewer' AND name=%s AND deleted_at IS NULL "
            "ORDER BY updated_at DESC LIMIT 1", (sow_id,))
        row = cur.fetchone()
    data = (row or {}).get("data") or {}
    if isinstance(data, str):
        try:
            data = _json_loads(data)
        except Exception:  # noqa: BLE001
            data = {}
    return {"sowId": sow_id, "reviewer": data or None}


class AssignReviewer(BaseModel):
    reviewerId: str
    reviewerEmail: str | None = None
    reviewerName: str | None = None


@router.post("/api/superadmin/sows/{sow_id}/assign-reviewer")
async def assign_sow_reviewer(sow_id: str, body: AssignReviewer, request: Request,
                             admin: Annotated[dict, Depends(get_current_admin)]):
    """Assign (or re-assign) the enterprise reviewer for a SOW at intake. Stored in
    admin_records (kind=sow_reviewer, name=sow_id). The reviewer queue (two-stage
    review hand-off) routes this SOW's accepted submissions to this reviewer."""
    conn = get_pg_connection()
    rid = f"sow_reviewer_{sow_id}"
    payload = {"reviewerId": body.reviewerId, "reviewerEmail": body.reviewerEmail, "reviewerName": body.reviewerName}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO admin_records (id, kind, name, status, data)
            VALUES (%s, 'sow_reviewer', %s, 'assigned', %s)
            ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, status='assigned',
                                           deleted_at = NULL, updated_at = now()
            """,
            (rid, sow_id, Json(payload)))
    conn.commit()
    write_audit(actor_id=str(admin.get("id", "")), actor_email=admin.get("email"),
                actor_role=admin.get("role"), action="assign_reviewer", target="sow",
                target_id=sow_id, service="superadmin-service",
                ip_address=request.client.host if request.client else None,
                extra={"reviewerId": body.reviewerId})
    return {"ok": True, "sowId": sow_id, "reviewer": payload}


def _json_loads(s):
    import json as _j
    return _j.loads(s)


@router.get("/api/superadmin/records/{kind}")
async def list_records(kind: str, admin: Annotated[dict, Depends(get_current_admin)]):
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM admin_records WHERE kind=%s AND deleted_at IS NULL ORDER BY created_at DESC",
            (kind,))
        return {"items": [_rec_out(r) for r in cur.fetchall()]}


@router.post("/api/superadmin/records/{kind}", status_code=201)
async def create_record(kind: str, request: Request,
                        admin: Annotated[dict, Depends(get_current_admin)],
                        body: dict | None = None):
    body = body or {}
    rid = body.get("id") or f"{kind}_{_uuid.uuid4().hex[:12]}"
    name = body.get("name"); status = body.get("status")
    extra = {k: v for k, v in body.items() if k not in ("id", "name", "status")}
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "INSERT INTO admin_records (id,kind,name,status,data) VALUES (%s,%s,%s,%s,%s) RETURNING *",
            (rid, kind, name, status, Json(extra)))
        row = cur.fetchone()
    conn.commit()
    write_audit(actor_id=str(admin.get("id", "")), actor_email=admin.get("email"),
                actor_role=admin.get("role"), action=f"{kind}_created", target=kind, target_id=rid,
                service="superadmin-service",
                ip_address=request.client.host if request.client else None)
    return _rec_out(row)


@router.patch("/api/superadmin/records/{kind}/{record_id}")
async def update_record(kind: str, record_id: str, request: Request,
                        admin: Annotated[dict, Depends(get_current_admin)],
                        body: dict | None = None):
    body = body or {}
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM admin_records WHERE id=%s AND kind=%s AND deleted_at IS NULL",
                    (record_id, kind))
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Record not found")
        data = dict(existing.get("data") or {})
        data.update({k: v for k, v in body.items() if k not in ("id", "name", "status")})
        cur.execute(
            "UPDATE admin_records SET name=COALESCE(%s,name), status=COALESCE(%s,status), data=%s, updated_at=now() WHERE id=%s RETURNING *",
            (body.get("name"), body.get("status"), Json(data), record_id))
        row = cur.fetchone()
    conn.commit()
    write_audit(actor_id=str(admin.get("id", "")), actor_email=admin.get("email"),
                actor_role=admin.get("role"), action=f"{kind}_updated", target=kind, target_id=record_id,
                service="superadmin-service",
                ip_address=request.client.host if request.client else None)
    return _rec_out(row)


@router.delete("/api/superadmin/records/{kind}/{record_id}")
async def delete_record(kind: str, record_id: str, request: Request,
                        admin: Annotated[dict, Depends(get_current_admin)]):
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE admin_records SET deleted_at=now() WHERE id=%s AND kind=%s AND deleted_at IS NULL",
            (record_id, kind))
        affected = cur.rowcount
    conn.commit()
    if affected == 0:
        raise HTTPException(status_code=404, detail="Record not found")
    write_audit(actor_id=str(admin.get("id", "")), actor_email=admin.get("email"),
                actor_role=admin.get("role"), action=f"{kind}_deleted", target=kind, target_id=record_id,
                service="superadmin-service",
                ip_address=request.client.host if request.client else None)
    return {"ok": True, "deleted_id": record_id}
