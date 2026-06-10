"""
Contributor domain router — /api/contributor/** plus the public credential
share endpoint /api/public/credentials/{shareId}.

All endpoints (except the public share) are Bearer-protected via
get_current_user. Many also accept an optional X-Contributor-Id header that
overrides the acting account id (used by admin tooling / impersonation). Bodies
are snake_case dicts returned directly (no success wrapper), per the contract.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Any

import pyotp
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from psycopg2.extras import Json

from shared.audit import write_audit
from shared.blob import blob_is_configured, upload_blob
from shared.deps import get_current_user
from shared.kafka_bus import publish_event
from shared.security import hash_password, verify_password

from contributor_app import db

router = APIRouter(prefix="/api/contributor", tags=["contributor"])
public_router = APIRouter(prefix="/api/public", tags=["contributor-public"])


# ── acting-account resolver (token + optional X-Contributor-Id override) ──────

def acting_account_id(
    user: Annotated[dict, Depends(get_current_user)],
    x_contributor_id: Annotated[str | None, Header(alias="X-Contributor-Id")] = None,
) -> int:
    raw = x_contributor_id or user.get("id")
    try:
        account_id = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid contributor id")
    db.seed_demo_data(account_id)
    return account_id


AcctId = Annotated[int, Depends(acting_account_id)]


def _require(row: dict[str, Any] | None, what: str = "Resource") -> dict[str, Any]:
    if row is None:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return row


# ════════════════════════════════════════════════════════════════════════════
# Dashboard
# ════════════════════════════════════════════════════════════════════════════

@router.get("/dashboard")
async def dashboard(account_id: AcctId):
    acct = db.get_account(account_id) or {}
    tasks = db.fetch_all("SELECT * FROM contributor_tasks WHERE account_id=%s", (account_id,))
    earnings = db.fetch_all("SELECT * FROM contributor_earnings WHERE account_id=%s", (account_id,))
    unread = db.fetch_one(
        "SELECT COUNT(*) AS c FROM contributor_notifications WHERE account_id=%s AND is_read=FALSE",
        (account_id,),
    )
    total_earned = sum(float(e.get("amount", 0)) for e in earnings if e.get("status") in ("cleared", "paid"))
    pending_earned = sum(float(e.get("amount", 0)) for e in earnings if e.get("status") == "pending")
    return {
        "contributor": {
            "id": str(account_id),
            "name": (acct.get("name") or "").strip() or acct.get("email", ""),
            "email": acct.get("email", ""),
        },
        "stats": {
            "active_tasks": len([t for t in tasks if t.get("status") in ("assigned", "in_progress")]),
            "available_tasks": len([t for t in tasks if t.get("status") == "available"]),
            "completed_tasks": len([t for t in tasks if t.get("status") == "completed"]),
            "total_earned": total_earned,
            "pending_earnings": pending_earned,
            "unread_notifications": int(unread["c"]) if unread else 0,
        },
        "recent_tasks": tasks[:5],
        "recent_earnings": earnings[:5],
    }


# ════════════════════════════════════════════════════════════════════════════
# Notifications
# ════════════════════════════════════════════════════════════════════════════

@router.get("/notifications")
async def list_notifications(account_id: AcctId, page: int = 1, page_size: int = 20):
    rows = db.fetch_all(
        "SELECT * FROM contributor_notifications WHERE account_id=%s ORDER BY created_at DESC",
        (account_id,),
    )
    result = db.paginate(rows, page, page_size)
    result["unread_count"] = len([r for r in rows if not r.get("is_read")])
    return result


@router.patch("/notifications/{notif_id}/read")
async def mark_notification_read(account_id: AcctId, notif_id: int):
    row = db.execute(
        "UPDATE contributor_notifications SET is_read=TRUE WHERE id=%s AND account_id=%s RETURNING *",
        (notif_id, account_id),
    )
    return _require(row, "Notification")


@router.post("/notifications/read-all")
async def mark_all_read(account_id: AcctId):
    db.execute(
        "UPDATE contributor_notifications SET is_read=TRUE WHERE account_id=%s", (account_id,)
    )
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════════════
# Settings
# ════════════════════════════════════════════════════════════════════════════

def _settings_row(account_id: int) -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM contributor_settings WHERE account_id=%s", (account_id,))
    if row is None:
        db.execute(
            "INSERT INTO contributor_settings (account_id, data) VALUES (%s, %s) "
            "ON CONFLICT (account_id) DO NOTHING",
            (account_id, Json({})),
        )
        row = db.fetch_one("SELECT * FROM contributor_settings WHERE account_id=%s", (account_id,))
    return row


@router.get("/settings")
async def get_settings(account_id: AcctId):
    return _settings_row(account_id)


def _merge_settings(account_id: int, section: str | None, payload: dict) -> dict[str, Any]:
    row = _settings_row(account_id)
    data = {k: v for k, v in row.items() if k not in ("account_id", "mfa_enabled", "mfa_secret", "updated_at")}
    data.pop("id", None)
    if section:
        merged = {**(data.get(section) or {}), **payload}
        data[section] = merged
    else:
        data.update(payload)
    db.execute(
        "UPDATE contributor_settings SET data=%s, updated_at=now() WHERE account_id=%s",
        (Json(data), account_id),
    )
    return _settings_row(account_id)


@router.patch("/settings")
async def update_settings(account_id: AcctId, payload: dict = Body(default={})):
    return _merge_settings(account_id, None, payload)


@router.patch("/settings/account")
async def update_settings_account(account_id: AcctId, payload: dict = Body(default={})):
    return _merge_settings(account_id, "account", payload)


@router.patch("/settings/notifications")
async def update_settings_notifications(account_id: AcctId, payload: dict = Body(default={})):
    return _merge_settings(account_id, "notifications", payload)


@router.patch("/settings/locale")
async def update_settings_locale(account_id: AcctId, payload: dict = Body(default={})):
    return _merge_settings(account_id, "locale", payload)


@router.post("/settings/security/2fa/setup")
async def twofa_setup(account_id: AcctId):
    acct = db.get_account(account_id) or {}
    row = _settings_row(account_id)
    secret = row.get("mfa_secret") or pyotp.random_base32()
    db.execute("UPDATE contributor_settings SET mfa_secret=%s WHERE account_id=%s", (secret, account_id))
    otpauth = pyotp.totp.TOTP(secret).provisioning_uri(
        name=acct.get("email", "contributor"), issuer_name="Glimmora"
    )
    return {"secret": secret, "otpauth_uri": otpauth}


@router.post("/settings/security/2fa/verify")
async def twofa_verify(account_id: AcctId, payload: dict = Body(default={})):
    code = str(payload.get("code", ""))
    row = _settings_row(account_id)
    secret = row.get("mfa_secret")
    if not secret:
        raise HTTPException(status_code=409, detail="Run 2fa/setup first")
    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid code")
    db.execute("UPDATE contributor_settings SET mfa_enabled=TRUE WHERE account_id=%s", (account_id,))
    db.execute("UPDATE login_accounts SET mfa_enabled=TRUE, mfa_secret=%s WHERE id=%s", (secret, account_id))
    return {"ok": True, "mfa_enabled": True}


@router.post("/settings/security/2fa/disable")
async def twofa_disable(account_id: AcctId):
    db.execute("UPDATE contributor_settings SET mfa_enabled=FALSE, mfa_secret=NULL WHERE account_id=%s",
               (account_id,))
    db.execute("UPDATE login_accounts SET mfa_enabled=FALSE, mfa_secret=NULL WHERE id=%s", (account_id,))
    return {"ok": True, "mfa_enabled": False}


@router.post("/settings/security/change-password")
async def change_password(account_id: AcctId, payload: dict = Body(default={})):
    acct = db.get_account(account_id)
    if not acct:
        raise HTTPException(status_code=404, detail="Account not found")
    new_password = payload.get("new_password") or payload.get("newPassword")
    if not new_password:
        raise HTTPException(status_code=422, detail="new_password is required")
    old_password = payload.get("old_password") or payload.get("oldPassword")
    if acct.get("is_password_set") and acct.get("password_hash") and old_password is not None:
        if not verify_password(old_password, acct["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
    db.execute(
        "UPDATE login_accounts SET password_hash=%s, is_password_set=TRUE, updated_at=now() WHERE id=%s",
        (hash_password(new_password), account_id),
    )
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════════════
# Tasks + Workroom
# ════════════════════════════════════════════════════════════════════════════

def _get_task(account_id: int, task_id: int) -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM contributor_tasks WHERE id=%s AND account_id=%s",
                       (task_id, account_id))
    return _require(row, "Task")


def _patch_task_data(account_id: int, task_id: int, mutate) -> dict[str, Any]:
    c = db.conn()
    from psycopg2.extras import RealDictCursor
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT data, status FROM contributor_tasks WHERE id=%s AND account_id=%s",
                    (task_id, account_id))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        data = dict(row["data"] or {})
        new_status = mutate(data)
        cur.execute(
            "UPDATE contributor_tasks SET data=%s, status=COALESCE(%s, status), updated_at=now() "
            "WHERE id=%s AND account_id=%s",
            (Json(data), new_status, task_id, account_id),
        )
    c.commit()
    return _get_task(account_id, task_id)


@router.get("/tasks")
async def list_tasks(
    account_id: AcctId,
    status: list[str] | None = Query(default=None),
    category: str | None = None,
    priority: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    page: int = 1,
    page_size: int = 20,
):
    rows = db.fetch_all(
        "SELECT * FROM contributor_tasks WHERE account_id=%s ORDER BY updated_at DESC NULLS LAST, created_at DESC",
        (account_id,),
    )
    if status:
        status_set = set(status)
        rows = [r for r in rows if r.get("status") in status_set]
    if category:
        rows = [r for r in rows if r.get("category") == category]
    if priority:
        rows = [r for r in rows if r.get("priority") == priority]

    # Normalise to the FE TaskDefinition shape so consumers never hit undefined fields.
    items = []
    for r in rows:
        d = r.get("data") if isinstance(r.get("data"), dict) else {}
        sub = db.fetch_one(
            "SELECT id, status, created_at, updated_at FROM contributor_submissions "
            "WHERE task_id=%s AND account_id=%s ORDER BY created_at DESC LIMIT 1",
            (r["id"], account_id),
        )
        items.append({
            "id": str(r["id"]),
            "title": r.get("title") or "",
            "externalKey": d.get("externalKey") or d.get("external_key"),
            "status": r.get("status") or "available",
            "requiredSkills": d.get("requiredSkills") or d.get("skills_required") or d.get("skills") or [],
            "estimatedHours": d.get("estimatedHours") or d.get("effort_hours") or 0,
            "complexity": d.get("complexity"),
            "acceptanceCriteria": d.get("acceptanceCriteria") or d.get("acceptance_criteria"),
            "agreedRatePerHour": r.get("reward"),
            "agreedCurrency": r.get("currency") or "USD",
            "assignedAt": d.get("assignedAt") or d.get("assigned_at"),
            "acceptedAt": d.get("acceptedAt") or d.get("accepted_at"),
            "updatedAt": r.get("updated_at"),
            "sow": d.get("sow"),
            "milestone": d.get("milestone"),
            "latestSubmission": {
                "id": str(sub["id"]),
                "version": sub.get("version", 1),
                "status": sub.get("status"),
                "submittedAt": sub.get("created_at"),
                "decidedAt": sub.get("updated_at"),
            } if sub else None,
            # Keep legacy fields for backward compat
            "category": r.get("category"),
            "priority": r.get("priority") or "medium",
            "due_at": r.get("due_at"),
        })

    return {"items": items[:limit]}


@router.get("/tasks/summary")
async def tasks_summary(account_id: AcctId):
    rows = db.fetch_all("SELECT status FROM contributor_tasks WHERE account_id=%s", (account_id,))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"total": len(rows), "by_status": counts}


@router.get("/tasks/discovery/summary")
async def tasks_discovery_summary(account_id: AcctId):
    rows = db.fetch_all(
        "SELECT * FROM contributor_tasks WHERE account_id=%s AND status='available'", (account_id,)
    )
    by_cat: dict[str, int] = {}
    for r in rows:
        cat = r.get("category") or "other"
        by_cat[cat] = by_cat.get(cat, 0) + 1
    return {"available_count": len(rows), "by_category": by_cat,
            "recommended": rows[:5]}


@router.get("/tasks/{task_id}")
async def get_task(account_id: AcctId, task_id: int):
    r = _get_task(account_id, task_id)
    d = r.get("data") if isinstance(r.get("data"), dict) else {}
    # Fetch all submissions for this task
    subs_raw = db.fetch_all(
        "SELECT * FROM contributor_submissions WHERE task_id=%s AND account_id=%s ORDER BY created_at DESC",
        (task_id, account_id),
    )
    subs = [
        {
            "id": str(s["id"]),
            "version": s.get("version", 1),
            "status": s.get("status"),
            "body": s.get("summary"),
            "payload": s.get("data") if isinstance(s.get("data"), dict) else {},
            "submittedAt": s.get("created_at"),
            "decidedAt": s.get("updated_at"),
            "decisionRationale": (s.get("data") if isinstance(s.get("data"), dict) else {}).get("decisionRationale"),
            "artifacts": (s.get("data") if isinstance(s.get("data"), dict) else {}).get("artifacts") or [],
        }
        for s in subs_raw
    ]
    return {
        "task": {
            "id": str(r["id"]),
            "title": r.get("title") or "",
            "externalKey": d.get("externalKey") or d.get("external_key"),
            "description": d.get("description") or r.get("description"),
            "status": r.get("status") or "available",
            "requiredSkills": d.get("requiredSkills") or d.get("skills_required") or d.get("skills") or [],
            "estimatedHours": d.get("estimatedHours") or d.get("effort_hours") or 0,
            "complexity": d.get("complexity"),
            "acceptanceCriteria": d.get("acceptanceCriteria") or d.get("acceptance_criteria"),
            "agreedRatePerHour": r.get("reward"),
            "agreedCurrency": r.get("currency") or "USD",
            "assignedAt": d.get("assignedAt") or d.get("assigned_at"),
            "acceptedAt": d.get("acceptedAt") or d.get("accepted_at"),
            "createdAt": r.get("created_at"),
            "updatedAt": r.get("updated_at"),
            "plan": d.get("plan"),
            "milestone": d.get("milestone"),
            "sow": d.get("sow"),
            "submissions": subs,
            # Legacy fields
            "category": r.get("category"),
            "priority": r.get("priority") or "medium",
            "due_at": r.get("due_at"),
            "checklist": d.get("checklist") or [],
            "workroom": d.get("workroom") or {},
            "timeline": d.get("timeline") or [],
        }
    }


@router.get("/tasks/{task_id}/workroom")
async def get_workroom(account_id: AcctId, task_id: int):
    task = _get_task(account_id, task_id)
    return task.get("workroom") or {"links": [], "templates": [], "checklist": task.get("checklist", [])}


@router.get("/tasks/{task_id}/workroom/links")
async def get_workroom_links(account_id: AcctId, task_id: int):
    task = _get_task(account_id, task_id)
    return {"links": (task.get("workroom") or {}).get("links", [])}


@router.get("/tasks/{task_id}/workroom/templates")
async def get_workroom_templates(account_id: AcctId, task_id: int):
    task = _get_task(account_id, task_id)
    return {"templates": (task.get("workroom") or {}).get("templates", [])}


@router.get("/tasks/{task_id}/workroom/messages")
async def get_workroom_messages(account_id: AcctId, task_id: int):
    _get_task(account_id, task_id)
    rows = db.fetch_all(
        "SELECT * FROM contributor_task_messages WHERE task_id=%s ORDER BY created_at ASC", (task_id,)
    )
    return {"items": rows}


@router.post("/tasks/{task_id}/workroom/messages")
async def post_workroom_message(account_id: AcctId, task_id: int, payload: dict = Body(default={})):
    _get_task(account_id, task_id)
    row = db.execute(
        "INSERT INTO contributor_task_messages (task_id, account_id, author, body, data) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (task_id, account_id, payload.get("author", "contributor"),
         payload.get("body", ""), Json(payload)),
    )
    return row


@router.post("/tasks/{task_id}/workroom/uploads")
async def upload_workroom_file(account_id: AcctId, task_id: int, file: UploadFile = File(...)):
    _get_task(account_id, task_id)
    contents = await file.read()
    if not blob_is_configured():
        raise HTTPException(status_code=503,
                            detail="Blob storage is not configured (BLOB_READ_WRITE_TOKEN missing)")
    descriptor = await upload_blob(
        pathname=f"contributor/{account_id}/tasks/{task_id}/{file.filename}",
        data=contents,
        content_type=file.content_type,
    )
    row = db.execute(
        "INSERT INTO contributor_uploads (account_id, task_id, filename, url, content_type, size_bytes, data) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (account_id, task_id, file.filename, descriptor.get("url", ""),
         file.content_type, len(contents), Json(descriptor)),
    )
    return row


@router.delete("/tasks/{task_id}/workroom/uploads/{upload_id}")
async def delete_workroom_upload(account_id: AcctId, task_id: int, upload_id: int):
    row = db.execute(
        "DELETE FROM contributor_uploads WHERE id=%s AND task_id=%s AND account_id=%s RETURNING id",
        (upload_id, task_id, account_id),
    )
    _require(row, "Upload")
    return {"ok": True, "deleted_id": upload_id}


@router.patch("/tasks/{task_id}/workroom/checklist/{item_id}")
async def patch_checklist_item(account_id: AcctId, task_id: int, item_id: str, payload: dict = Body(default={})):
    def mutate(data: dict):
        checklist = data.get("checklist")
        if checklist is None:
            checklist = (data.get("workroom") or {}).get("checklist", [])
        for item in checklist:
            if str(item.get("id")) == str(item_id):
                if "done" in payload:
                    item["done"] = bool(payload["done"])
                if "label" in payload:
                    item["label"] = payload["label"]
        data["checklist"] = checklist
        return None
    return _patch_task_data(account_id, task_id, mutate)


@router.get("/tasks/{task_id}/timeline")
async def get_timeline(account_id: AcctId, task_id: int):
    task = _get_task(account_id, task_id)
    return {"items": task.get("timeline") or []}


def _append_timeline(account_id: int, task_id: int, event: str, status: str | None = None):
    from datetime import datetime, timezone

    def mutate(data: dict):
        timeline = data.get("timeline") or []
        timeline.append({"event": event, "at": datetime.now(timezone.utc).isoformat()})
        data["timeline"] = timeline
        return status
    return _patch_task_data(account_id, task_id, mutate)


@router.post("/tasks/{task_id}/request-extension")
async def request_extension(account_id: AcctId, task_id: int, payload: dict = Body(default={})):
    def mutate(data: dict):
        data["extension_request"] = payload
        return None
    _patch_task_data(account_id, task_id, mutate)
    return _append_timeline(account_id, task_id, "extension_requested")


@router.post("/tasks/{task_id}/start")
async def start_task(account_id: AcctId, task_id: int):
    return _append_timeline(account_id, task_id, "started", status="in_progress")


@router.post("/tasks/{task_id}/decline")
async def decline_task(account_id: AcctId, task_id: int, payload: dict = Body(default={})):
    def mutate(data: dict):
        data["decline_reason"] = payload.get("reason", "")
        return "declined"
    return _patch_task_data(account_id, task_id, mutate)


@router.post("/tasks/{task_id}/accept")
async def accept_task(account_id: AcctId, task_id: int):
    return _append_timeline(account_id, task_id, "accepted", status="assigned")


@router.get("/tasks/{task_id}/accept-impact")
async def accept_impact(account_id: AcctId, task_id: int):
    task = _get_task(account_id, task_id)
    active = db.fetch_one(
        "SELECT COUNT(*) AS c FROM contributor_tasks WHERE account_id=%s AND status IN ('assigned','in_progress')",
        (account_id,),
    )
    return {
        "task_id": task_id,
        "reward": task.get("reward", 0),
        "current_active_tasks": int(active["c"]) if active else 0,
        "estimated_weekly_hours": (task.get("data") or {}).get("estimated_hours", 4),
        "conflicts": [],
    }


@router.get("/tasks/{task_id}/review-feedback")
async def review_feedback(account_id: AcctId, task_id: int):
    sub = db.fetch_one(
        "SELECT * FROM contributor_submissions WHERE task_id=%s AND account_id=%s ORDER BY created_at DESC",
        (task_id, account_id),
    )
    return {"task_id": task_id, "feedback": (sub or {}).get("feedback") if sub else None,
            "status": (sub or {}).get("status") if sub else None}


@router.get("/tasks/{task_id}/latest-submission")
async def latest_submission(account_id: AcctId, task_id: int):
    sub = db.fetch_one(
        "SELECT * FROM contributor_submissions WHERE task_id=%s AND account_id=%s ORDER BY created_at DESC",
        (task_id, account_id),
    )
    return sub or {}


@router.post("/tasks/{task_id}/submissions")
async def create_task_submission(account_id: AcctId, task_id: int, payload: dict = Body(default={})):
    task = _get_task(account_id, task_id)
    row = db.execute(
        "INSERT INTO contributor_submissions (account_id, task_id, status, summary, data) "
        "VALUES (%s,%s,'submitted',%s,%s) RETURNING *",
        (account_id, task_id, payload.get("summary", ""), Json(payload)),
    )
    db.execute("UPDATE contributor_tasks SET status='submitted', updated_at=now() WHERE id=%s AND account_id=%s",
               (task_id, account_id))
    publish_event("contributor.submission_created",
                  {"accountId": str(account_id), "taskId": task_id, "submissionId": row["id"]})
    # Two-stage review routing: external contributors go to the MENTOR queue
    # first (Glimmora QA), then the enterprise reviewer. Internal employees skip
    # the mentor. We create a pending mentor_reviews row here (shared DB) so the
    # submission shows up in the mentor queue. Failures are non-fatal.
    try:
        acct = db.fetch_one("SELECT email, first_name, last_name, role, department FROM login_accounts WHERE id=%s",
                            (account_id,))
        is_internal = bool(acct and (acct.get("role") == "employee"
                                     or (acct.get("department") or "").lower() == "internal"))
        if not is_internal:
            name = (f"{(acct or {}).get('first_name','') or ''} {(acct or {}).get('last_name','') or ''}".strip()
                    or (acct or {}).get("email") or f"Contributor {account_id}")
            title = (task or {}).get("title") or payload.get("summary") or "Task submission"
            url = payload.get("url") or payload.get("deliverableUrl")
            # Route to the mentor the Super Admin ASSIGNED to this task's SOW. If
            # none assigned, fall back to the open 'pool' so nothing gets stuck.
            mentor_id, mentor_email = "pool", None
            try:
                # The DB layer spreads the JSONB `data` into top-level row keys,
                # so sowId may live at task["sowId"] OR inside task["data"].
                tdata = (task or {}).get("data") or {}
                if isinstance(tdata, str):
                    import json as _j; tdata = _j.loads(tdata)
                sow_id = (task or {}).get("sowId") or tdata.get("sowId")
                if sow_id:
                    rec = db.fetch_one(
                        "SELECT data FROM admin_records WHERE kind='sow_mentor' AND name=%s "
                        "AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 1", (sow_id,))
                    # The DB layer spreads JSONB `data` into the row, so mentorId
                    # may be a top-level key OR inside rec["data"].
                    rec = rec or {}
                    rdata = rec.get("data") if isinstance(rec.get("data"), dict) else rec
                    if isinstance(rdata, str):
                        import json as _j; rdata = _j.loads(rdata)
                    if isinstance(rdata, dict) and rdata.get("mentorId"):
                        mentor_id = str(rdata["mentorId"]); mentor_email = rdata.get("mentorEmail")
            except Exception:  # noqa: BLE001
                mentor_id, mentor_email = "pool", None
            db.execute(
                "INSERT INTO mentor_reviews "
                "  (mentor_id, mentor_email, title, submission_type, contributor_id, contributor_name, "
                "   priority, status, payload) "
                "VALUES (%s,%s,%s,'assignment',%s,%s,'normal','pending',%s)",
                (mentor_id, mentor_email, title, str(account_id), name,
                 Json({"taskId": task_id, "submissionId": row["id"], "accountId": account_id,
                       "url": url, "summary": payload.get("summary", ""), "stage": "mentor"})),
            )
    except Exception:  # noqa: BLE001 — review-queue wiring must never block submit
        pass
    return row


# ════════════════════════════════════════════════════════════════════════════
# Submissions
# ════════════════════════════════════════════════════════════════════════════

@router.get("/submissions")
async def list_submissions(account_id: AcctId, status: str | None = None, page: int = 1, page_size: int = 20):
    rows = db.fetch_all(
        "SELECT * FROM contributor_submissions WHERE account_id=%s ORDER BY created_at DESC", (account_id,)
    )
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return db.paginate(rows, page, page_size)


@router.get("/submissions/{submission_id}")
async def get_submission(account_id: AcctId, submission_id: int):
    return _require(db.fetch_one(
        "SELECT * FROM contributor_submissions WHERE id=%s AND account_id=%s", (submission_id, account_id)
    ), "Submission")


@router.patch("/submissions/{submission_id}")
async def patch_submission(account_id: AcctId, submission_id: int, payload: dict = Body(default={})):
    existing = _require(db.fetch_one(
        "SELECT * FROM contributor_submissions WHERE id=%s AND account_id=%s", (submission_id, account_id)
    ), "Submission")
    merged = {**(existing.get("data") if isinstance(existing.get("data"), dict) else {}), **payload}
    row = db.execute(
        "UPDATE contributor_submissions SET summary=COALESCE(%s, summary), data=%s, updated_at=now() "
        "WHERE id=%s AND account_id=%s RETURNING *",
        (payload.get("summary"), Json(merged), submission_id, account_id),
    )
    return row


@router.post("/submissions/{submission_id}/resubmit")
async def resubmit_submission(account_id: AcctId, submission_id: int, payload: dict = Body(default={})):
    row = db.execute(
        "UPDATE contributor_submissions SET status='submitted', summary=COALESCE(%s, summary), updated_at=now() "
        "WHERE id=%s AND account_id=%s RETURNING *",
        (payload.get("summary"), submission_id, account_id),
    )
    return _require(row, "Submission")


# ════════════════════════════════════════════════════════════════════════════
# Earnings + Payouts
# ════════════════════════════════════════════════════════════════════════════

def _earnings_rows(account_id: int) -> list[dict[str, Any]]:
    return db.fetch_all(
        "SELECT * FROM contributor_earnings WHERE account_id=%s ORDER BY created_at DESC", (account_id,)
    )


@router.get("/earnings/summary")
async def earnings_summary(account_id: AcctId):
    rows = _earnings_rows(account_id)
    cleared = sum(float(r["amount"]) for r in rows if r["status"] in ("cleared", "paid"))
    pending = sum(float(r["amount"]) for r in rows if r["status"] == "pending")
    return {"total_earned": cleared, "pending": pending, "lifetime": cleared + pending,
            "currency": "USD", "count": len(rows)}


@router.get("/earnings/overview")
async def earnings_overview(account_id: AcctId):
    rows = _earnings_rows(account_id)
    by_status: dict[str, float] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + float(r["amount"])
    return {"by_status": by_status, "recent": rows[:10], "currency": "USD"}


@router.get("/earnings/chart")
async def earnings_chart(account_id: AcctId):
    rows = _earnings_rows(account_id)
    monthly: dict[str, float] = {}
    for r in rows:
        month = str(r.get("created_at", ""))[:7]
        monthly[month] = monthly.get(month, 0) + float(r["amount"])
    series = [{"period": k, "amount": v} for k, v in sorted(monthly.items())]
    return {"series": series, "currency": "USD"}


@router.get("/earnings/kyc/status")
async def kyc_status(account_id: AcctId):
    row = db.fetch_one("SELECT * FROM contributor_kyc WHERE account_id=%s", (account_id,))
    if row is None:
        db.execute("INSERT INTO contributor_kyc (account_id, status, data) VALUES (%s,'not_started',%s) "
                   "ON CONFLICT (account_id) DO NOTHING", (account_id, Json({})))
        row = db.fetch_one("SELECT * FROM contributor_kyc WHERE account_id=%s", (account_id,))
    return row


@router.post("/earnings/kyc/start")
async def kyc_start(account_id: AcctId, payload: dict = Body(default={})):
    row = db.execute(
        "INSERT INTO contributor_kyc (account_id, status, data) VALUES (%s,'pending',%s) "
        "ON CONFLICT (account_id) DO UPDATE SET status='pending', data=EXCLUDED.data, updated_at=now() "
        "RETURNING *",
        (account_id, Json(payload)),
    )
    return row


@router.get("/earnings/{earning_id}")
async def get_earning(account_id: AcctId, earning_id: int):
    return _require(db.fetch_one(
        "SELECT * FROM contributor_earnings WHERE id=%s AND account_id=%s", (earning_id, account_id)
    ), "Earning")


@router.get("/earnings")
async def list_earnings(account_id: AcctId, status: str | None = None, page: int = 1, page_size: int = 20):
    rows = _earnings_rows(account_id)
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return db.paginate(rows, page, page_size)


@router.get("/payouts")
async def list_payouts(account_id: AcctId, page: int = 1, page_size: int = 20):
    rows = db.fetch_all(
        "SELECT * FROM contributor_payouts WHERE account_id=%s ORDER BY created_at DESC", (account_id,)
    )
    return db.paginate(rows, page, page_size)


@router.get("/payouts/{payout_id}")
async def get_payout(account_id: AcctId, payout_id: int):
    return _require(db.fetch_one(
        "SELECT * FROM contributor_payouts WHERE id=%s AND account_id=%s", (payout_id, account_id)
    ), "Payout")


@router.get("/payouts/{payout_id}/receipt")
async def get_payout_receipt(account_id: AcctId, payout_id: int):
    row = _require(db.fetch_one(
        "SELECT * FROM contributor_payouts WHERE id=%s AND account_id=%s", (payout_id, account_id)
    ), "Payout")
    acct = db.get_account(account_id) or {}
    return {
        "receipt_id": f"RCPT-{payout_id}",
        "payout_id": payout_id,
        "amount": row.get("amount"),
        "currency": row.get("currency"),
        "status": row.get("status"),
        "method": row.get("method"),
        "reference": row.get("reference"),
        "issued_to": acct.get("email"),
        "issued_at": row.get("created_at"),
    }


@router.get("/payout-preferences")
async def get_payout_preferences(account_id: AcctId):
    row = db.fetch_one("SELECT * FROM contributor_payout_preferences WHERE account_id=%s", (account_id,))
    if row is None:
        db.execute("INSERT INTO contributor_payout_preferences (account_id, data) VALUES (%s,%s) "
                   "ON CONFLICT (account_id) DO NOTHING", (account_id, Json({})))
        row = db.fetch_one("SELECT * FROM contributor_payout_preferences WHERE account_id=%s", (account_id,))
    return row


@router.put("/payout-preferences")
async def update_payout_preferences(account_id: AcctId, payload: dict = Body(default={})):
    row = db.execute(
        "INSERT INTO contributor_payout_preferences (account_id, data) VALUES (%s,%s) "
        "ON CONFLICT (account_id) DO UPDATE SET data=EXCLUDED.data, updated_at=now() RETURNING *",
        (account_id, Json(payload)),
    )
    return row


# ════════════════════════════════════════════════════════════════════════════
# Messages (threads)
# ════════════════════════════════════════════════════════════════════════════

@router.get("/messages/threads")
async def list_threads(account_id: AcctId, page: int = 1, page_size: int = 20):
    rows = db.fetch_all(
        "SELECT * FROM contributor_message_threads WHERE account_id=%s ORDER BY updated_at DESC", (account_id,)
    )
    return db.paginate(rows, page, page_size)


@router.get("/messages/threads/{thread_id}")
async def get_thread(account_id: AcctId, thread_id: int):
    thread = _require(db.fetch_one(
        "SELECT * FROM contributor_message_threads WHERE id=%s AND account_id=%s", (thread_id, account_id)
    ), "Thread")
    thread["messages"] = db.fetch_all(
        "SELECT * FROM contributor_thread_messages WHERE thread_id=%s ORDER BY created_at ASC", (thread_id,)
    )
    return thread


@router.post("/messages/threads/{thread_id}/messages")
async def post_thread_message(account_id: AcctId, thread_id: int, payload: dict = Body(default={})):
    _require(db.fetch_one(
        "SELECT id FROM contributor_message_threads WHERE id=%s AND account_id=%s", (thread_id, account_id)
    ), "Thread")
    row = db.execute(
        "INSERT INTO contributor_thread_messages (thread_id, account_id, author, body, data) "
        "VALUES (%s,%s,'contributor',%s,%s) RETURNING *",
        (thread_id, account_id, payload.get("body", ""), Json(payload)),
    )
    db.execute("UPDATE contributor_message_threads SET updated_at=now() WHERE id=%s", (thread_id,))
    return row


@router.post("/messages/threads/{thread_id}/read")
async def mark_thread_read(account_id: AcctId, thread_id: int):
    db.execute(
        "UPDATE contributor_thread_messages SET is_read=TRUE WHERE thread_id=%s AND account_id=%s",
        (thread_id, account_id),
    )
    return {"ok": True}


@router.post("/messages/{message_id}/rating")
async def rate_message(account_id: AcctId, message_id: int, payload: dict = Body(default={})):
    rating = payload.get("rating")
    row = db.execute(
        "UPDATE contributor_thread_messages SET rating=%s WHERE id=%s RETURNING *",
        (rating, message_id),
    )
    return _require(row, "Message")


# ════════════════════════════════════════════════════════════════════════════
# Learning
# ════════════════════════════════════════════════════════════════════════════

@router.get("/learning/recommendations")
async def learning_recommendations(account_id: AcctId):
    rows = db.fetch_all(
        "SELECT * FROM contributor_learning WHERE account_id=%s AND status!='dismissed' ORDER BY created_at DESC",
        (account_id,),
    )
    return {"items": rows}


@router.post("/learning/recommendations/{rec_id}/dismiss")
async def dismiss_recommendation(account_id: AcctId, rec_id: int):
    row = db.execute(
        "UPDATE contributor_learning SET status='dismissed' WHERE id=%s AND account_id=%s RETURNING *",
        (rec_id, account_id),
    )
    return _require(row, "Recommendation")


@router.post("/learning/recommendations/{rec_id}/mark-opened")
async def mark_recommendation_opened(account_id: AcctId, rec_id: int):
    row = db.execute(
        "UPDATE contributor_learning SET status='opened' WHERE id=%s AND account_id=%s RETURNING *",
        (rec_id, account_id),
    )
    return _require(row, "Recommendation")


# ════════════════════════════════════════════════════════════════════════════
# Support (tickets + grievances + safety reports + faqs)
# ════════════════════════════════════════════════════════════════════════════

@router.get("/support/tickets")
async def list_tickets(account_id: AcctId, page: int = 1, page_size: int = 20):
    rows = db.fetch_all(
        "SELECT * FROM contributor_support_tickets WHERE account_id=%s AND kind='ticket' ORDER BY created_at DESC",
        (account_id,),
    )
    return db.paginate(rows, page, page_size)


@router.post("/support/tickets")
async def create_ticket(account_id: AcctId, payload: dict = Body(default={})):
    row = db.execute(
        "INSERT INTO contributor_support_tickets (account_id, kind, subject, priority, data) "
        "VALUES (%s,'ticket',%s,%s,%s) RETURNING *",
        (account_id, payload.get("subject", ""), payload.get("priority", "normal"), Json(payload)),
    )
    return row


@router.get("/support/tickets/{ticket_id}")
async def get_ticket(account_id: AcctId, ticket_id: int):
    ticket = _require(db.fetch_one(
        "SELECT * FROM contributor_support_tickets WHERE id=%s AND account_id=%s AND kind='ticket'",
        (ticket_id, account_id),
    ), "Ticket")
    ticket["messages"] = db.fetch_all(
        "SELECT * FROM contributor_support_messages WHERE ticket_id=%s ORDER BY created_at ASC", (ticket_id,)
    )
    return ticket


@router.patch("/support/tickets/{ticket_id}")
async def patch_ticket(account_id: AcctId, ticket_id: int, payload: dict = Body(default={})):
    row = db.execute(
        "UPDATE contributor_support_tickets SET status=COALESCE(%s, status), "
        "subject=COALESCE(%s, subject), updated_at=now() "
        "WHERE id=%s AND account_id=%s AND kind='ticket' RETURNING *",
        (payload.get("status"), payload.get("subject"), ticket_id, account_id),
    )
    return _require(row, "Ticket")


@router.post("/support/tickets/{ticket_id}/messages")
async def post_ticket_message(account_id: AcctId, ticket_id: int, payload: dict = Body(default={})):
    _require(db.fetch_one(
        "SELECT id FROM contributor_support_tickets WHERE id=%s AND account_id=%s", (ticket_id, account_id)
    ), "Ticket")
    row = db.execute(
        "INSERT INTO contributor_support_messages (ticket_id, account_id, author, body) "
        "VALUES (%s,%s,'contributor',%s) RETURNING *",
        (ticket_id, account_id, payload.get("body", "")),
    )
    db.execute("UPDATE contributor_support_tickets SET updated_at=now() WHERE id=%s", (ticket_id,))
    return row


@router.get("/support/grievances")
async def list_grievances(account_id: AcctId, page: int = 1, page_size: int = 20):
    rows = db.fetch_all(
        "SELECT * FROM contributor_support_tickets WHERE account_id=%s AND kind='grievance' ORDER BY created_at DESC",
        (account_id,),
    )
    return db.paginate(rows, page, page_size)


@router.post("/support/grievances")
async def create_grievance(account_id: AcctId, payload: dict = Body(default={})):
    row = db.execute(
        "INSERT INTO contributor_support_tickets (account_id, kind, subject, priority, data) "
        "VALUES (%s,'grievance',%s,%s,%s) RETURNING *",
        (account_id, payload.get("subject", ""), payload.get("priority", "high"), Json(payload)),
    )
    return row


@router.get("/support/grievances/{grievance_id}")
async def get_grievance(account_id: AcctId, grievance_id: int):
    return _require(db.fetch_one(
        "SELECT * FROM contributor_support_tickets WHERE id=%s AND account_id=%s AND kind='grievance'",
        (grievance_id, account_id),
    ), "Grievance")


@router.post("/support/safety-reports")
async def create_safety_report(account_id: AcctId, payload: dict = Body(default={})):
    row = db.execute(
        "INSERT INTO contributor_support_tickets (account_id, kind, subject, priority, data) "
        "VALUES (%s,'safety_report',%s,'urgent',%s) RETURNING *",
        (account_id, payload.get("subject", "Safety report"), Json(payload)),
    )
    write_audit(actor_id=str(account_id), actor_email=None, actor_role="contributor",
                action="safety_report_filed", service="contributor-service",
                target_id=str(row["id"]))
    return row


@router.get("/support/faqs")
async def support_faqs(account_id: AcctId):
    return {"items": [
        {"id": "faq1", "question": "How do I get paid?",
         "answer": "Complete a task, get it approved, then request a payout once your balance clears."},
        {"id": "faq2", "question": "How do I accept a task?",
         "answer": "Open the task and click Accept; check the accept-impact preview first."},
        {"id": "faq3", "question": "What is KYC?",
         "answer": "Identity verification required before your first payout."},
    ]}


# ════════════════════════════════════════════════════════════════════════════
# Credentials + wallet + shares
# ════════════════════════════════════════════════════════════════════════════

@router.get("/credentials")
async def list_credentials(
    account_id: AcctId,
    skill: str | None = None,
    date_filter: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    rows = db.fetch_all(
        "SELECT * FROM contributor_credentials WHERE account_id=%s ORDER BY issued_at DESC", (account_id,)
    )
    # skill filter — match against skill tags stored inside data.skills[]
    if skill:
        skill_lower = skill.lower()
        filtered = []
        for r in rows:
            d = r.get("data") if isinstance(r.get("data"), dict) else {}
            tags = [str(s).lower() for s in (d.get("skills") or [])]
            if skill_lower in tags or skill_lower in str(r.get("title", "")).lower():
                filtered.append(r)
        rows = filtered
    # date_filter: "30d" | "90d" | "6m"
    if date_filter:
        from datetime import timedelta
        delta_map = {"30d": 30, "90d": 90, "6m": 180}
        days = delta_map.get(date_filter)
        if days:
            cutoff = db._now().isoformat()
            from datetime import timedelta as _td
            cutoff_dt = db._now() - _td(days=days)
            filtered = []
            for r in rows:
                issued = r.get("issued_at") or ""
                if issued and str(issued) >= cutoff_dt.isoformat()[:10]:
                    filtered.append(r)
            rows = filtered
    return db.paginate(rows, page, page_size)


@router.get("/credentials/wallet/summary")
async def wallet_summary(account_id: AcctId):
    rows = db.fetch_all("SELECT status FROM contributor_credentials WHERE account_id=%s", (account_id,))
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {"total": len(rows), "by_status": by_status}


@router.post("/credentials/wallet/summary", status_code=201)
async def wallet_summary_post(account_id: AcctId, payload: dict = Body(default={})):
    """Store a custom wallet summary annotation (e.g. a category grouping) linked to
    the current contributor. Requires `category` field. Persisted in a credential row
    with kind='wallet_summary'."""
    category = payload.get("category")
    if not category:
        raise HTTPException(status_code=422, detail="Category field is required.")
    row = db.execute(
        "INSERT INTO contributor_credentials (account_id, title, issuer, kind, status, data) "
        "VALUES (%s, %s, 'Glimmora', 'wallet_summary', 'issued', %s) RETURNING *",
        (account_id, f"Wallet summary — {category}", Json(payload)),
    )
    write_audit(
        actor_id=str(account_id), actor_email=None, actor_role="contributor",
        action="wallet_summary_created", service="contributor-service",
        target_id=str(row["id"]),
        extra={"category": category},
    )
    return row


@router.get("/credentials/wallet/cards")
async def wallet_cards(account_id: AcctId):
    rows = db.fetch_all(
        "SELECT * FROM contributor_credentials WHERE account_id=%s ORDER BY issued_at DESC", (account_id,)
    )
    return {"cards": rows}


@router.get("/credentials/skills/verification")
async def skills_verification(account_id: AcctId):
    creds = db.fetch_all(
        "SELECT * FROM contributor_credentials WHERE account_id=%s", (account_id,)
    )
    skills: dict[str, str] = {}
    for c in creds:
        for s in (c.get("data") or {}).get("skills", []) if isinstance(c.get("data"), dict) else []:
            skills[s] = "verified"
    return {"verified_skills": skills, "credentials_count": len(creds)}


def _get_credential(account_id: int, credential_id: int) -> dict[str, Any]:
    return _require(db.fetch_one(
        "SELECT * FROM contributor_credentials WHERE id=%s AND account_id=%s", (credential_id, account_id)
    ), "Credential")


@router.get("/credentials/{credential_id}")
async def get_credential(account_id: AcctId, credential_id: int):
    return _get_credential(account_id, credential_id)


@router.get("/credentials/{credential_id}/certificate")
async def get_certificate(account_id: AcctId, credential_id: int):
    cred = _get_credential(account_id, credential_id)
    acct = db.get_account(account_id) or {}
    return {
        "credential_id": credential_id,
        "title": cred.get("title"),
        "issuer": cred.get("issuer"),
        "holder": (acct.get("name") or "").strip() or acct.get("email"),
        "verification_code": cred.get("verification_code"),
        "issued_at": cred.get("issued_at"),
        "certificate_url": (cred.get("data") or {}).get("certificate_url"),
    }


@router.get("/credentials/{credential_id}/verification")
async def get_verification(account_id: AcctId, credential_id: int):
    cred = _get_credential(account_id, credential_id)
    return {
        "credential_id": credential_id,
        "verification_code": cred.get("verification_code"),
        "status": cred.get("status"),
        "verified": cred.get("status") == "issued",
    }


@router.post("/credentials/{credential_id}/share")
async def share_credential(account_id: AcctId, credential_id: int, payload: dict = Body(default={})):
    _get_credential(account_id, credential_id)
    share_id = secrets.token_urlsafe(10)
    row = db.execute(
        "INSERT INTO credential_shares (share_id, credential_id, account_id, data) "
        "VALUES (%s,%s,%s,%s) RETURNING *",
        (share_id, credential_id, account_id, Json(payload)),
    )
    row["share_url"] = f"/api/public/credentials/{share_id}"
    return row


@router.post("/credentials/{credential_id}/academic-portfolio")
async def add_to_academic_portfolio(account_id: AcctId, credential_id: int, payload: dict = Body(default={})):
    cred = _get_credential(account_id, credential_id)
    data = cred.get("data") if isinstance(cred.get("data"), dict) else {}
    data["academic_portfolio"] = {**(data.get("academic_portfolio") or {}), **payload, "added": True}
    row = db.execute(
        "UPDATE contributor_credentials SET data=%s WHERE id=%s AND account_id=%s RETURNING *",
        (Json(data), credential_id, account_id),
    )
    return row


# ════════════════════════════════════════════════════════════════════════════
# Profile + evidence + digital twin + search
# ════════════════════════════════════════════════════════════════════════════

def _profile(account_id: int) -> dict[str, Any]:
    acct = db.get_account(account_id) or {}
    prof = db.fetch_one("SELECT * FROM contributor_profiles WHERE account_id=%s", (account_id,)) or {}
    return {
        "id": str(account_id),
        "email": acct.get("email"),
        "first_name": acct.get("first_name"),
        "last_name": acct.get("last_name"),
        "name": acct.get("name"),
        "phone": acct.get("phone"),
        "bio": prof.get("bio"),
        "job_title": prof.get("job_title"),
        "country": prof.get("country"),
        "city": prof.get("city"),
        "timezone": prof.get("timezone"),
        "primary_skills": prof.get("primary_skills") or [],
        "secondary_skills": prof.get("secondary_skills") or [],
        "other_skills": prof.get("other_skills") or [],
        "linkedin": prof.get("linkedin"),
        "availability": prof.get("availability"),
    }


@router.get("/profile")
async def get_profile(account_id: AcctId):
    return _profile(account_id)


@router.patch("/profile")
async def patch_profile(account_id: AcctId, payload: dict = Body(default={})):
    # account-level fields
    if any(k in payload for k in ("first_name", "last_name", "phone")):
        db.execute(
            "UPDATE login_accounts SET first_name=COALESCE(%s, first_name), "
            "last_name=COALESCE(%s, last_name), phone=COALESCE(%s, phone), "
            "name=TRIM(COALESCE(%s, first_name) || ' ' || COALESCE(%s, last_name)), updated_at=now() "
            "WHERE id=%s",
            (payload.get("first_name"), payload.get("last_name"), payload.get("phone"),
             payload.get("first_name"), payload.get("last_name"), account_id),
        )
    # ensure a profile row exists, then update profile-level fields
    db.execute("INSERT INTO contributor_profiles (account_id) VALUES (%s) ON CONFLICT (account_id) DO NOTHING",
               (account_id,))
    db.execute(
        "UPDATE contributor_profiles SET bio=COALESCE(%s, bio), job_title=COALESCE(%s, job_title), "
        "country=COALESCE(%s, country), city=COALESCE(%s, city), timezone=COALESCE(%s, timezone), "
        "linkedin=COALESCE(%s, linkedin), availability=COALESCE(%s, availability), updated_at=now() "
        "WHERE account_id=%s",
        (payload.get("bio"), payload.get("job_title"), payload.get("country"), payload.get("city"),
         payload.get("timezone"), payload.get("linkedin"), payload.get("availability"), account_id),
    )
    return _profile(account_id)


@router.put("/profile/skills")
async def update_skills(account_id: AcctId, payload: dict = Body(default={})):
    db.execute("INSERT INTO contributor_profiles (account_id) VALUES (%s) ON CONFLICT (account_id) DO NOTHING",
               (account_id,))
    db.execute(
        "UPDATE contributor_profiles SET primary_skills=%s, secondary_skills=%s, other_skills=%s, updated_at=now() "
        "WHERE account_id=%s",
        (payload.get("primary_skills") or [], payload.get("secondary_skills") or [],
         payload.get("other_skills") or [], account_id),
    )
    return _profile(account_id)


@router.get("/profile/evidence")
async def list_evidence(account_id: AcctId):
    rows = db.fetch_all(
        "SELECT * FROM contributor_evidence WHERE account_id=%s AND status!='deleted' ORDER BY created_at DESC",
        (account_id,),
    )
    return {"items": rows}


@router.post("/profile/evidence")
async def create_evidence(account_id: AcctId, payload: dict = Body(default={})):
    row = db.execute(
        "INSERT INTO contributor_evidence (account_id, title, kind, url, data) VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (account_id, payload.get("title", ""), payload.get("kind", "link"),
         payload.get("url"), Json(payload)),
    )
    return row


@router.patch("/profile/evidence/{evidence_id}")
async def patch_evidence(account_id: AcctId, evidence_id: int, payload: dict = Body(default={})):
    row = db.execute(
        "UPDATE contributor_evidence SET title=COALESCE(%s, title), kind=COALESCE(%s, kind), "
        "url=COALESCE(%s, url) WHERE id=%s AND account_id=%s RETURNING *",
        (payload.get("title"), payload.get("kind"), payload.get("url"), evidence_id, account_id),
    )
    return _require(row, "Evidence")


@router.delete("/profile/evidence/{evidence_id}")
async def delete_evidence(account_id: AcctId, evidence_id: int):
    row = db.execute(
        "UPDATE contributor_evidence SET status='deleted' WHERE id=%s AND account_id=%s RETURNING id",
        (evidence_id, account_id),
    )
    _require(row, "Evidence")
    return {"ok": True, "deleted_id": evidence_id}


def _build_digital_twin(account_id: int) -> dict[str, Any]:
    profile = _profile(account_id)
    tasks = db.fetch_all("SELECT status FROM contributor_tasks WHERE account_id=%s", (account_id,))
    creds = db.fetch_all("SELECT id FROM contributor_credentials WHERE account_id=%s", (account_id,))
    earnings = _earnings_rows(account_id)
    return {
        "account_id": str(account_id),
        "skills": (profile.get("primary_skills") or []) + (profile.get("secondary_skills") or []),
        "tasks_completed": len([t for t in tasks if t.get("status") == "completed"]),
        "credentials_count": len(creds),
        "total_earned": sum(float(e["amount"]) for e in earnings if e["status"] in ("cleared", "paid")),
        "profile_completeness": _completeness(profile),
    }


def _completeness(profile: dict) -> int:
    """Backward-compatible basic completeness (used by digital twin)."""
    fields = ["bio", "job_title", "country", "timezone", "primary_skills", "linkedin"]
    filled = sum(1 for f in fields if profile.get(f))
    return int(filled / len(fields) * 100)


# ── Profile completion (skills + projects + experience + education + basics) ──
# Required for contributor-umbrella personas (freelancer/student/internal/women)
# to access the dashboard. Each section contributes equally; 100% = all done.

def _ensure_profile(account_id: int) -> None:
    """Seeded accounts (e.g. service users) may have no contributor_profiles row.
    Auto-create an empty one so profile writes/UPDATEs aren't silent no-ops."""
    db.execute(
        "INSERT INTO contributor_profiles (account_id) VALUES (%s) ON CONFLICT (account_id) DO NOTHING",
        (account_id,))


def _completion_status(account_id: int) -> dict[str, Any]:
    _ensure_profile(account_id)
    # Read the raw profile row directly (the mapped _profile() dict omits the
    # newer columns like expertise_areas).
    profile = db.fetch_one("SELECT * FROM contributor_profiles WHERE account_id=%s", (account_id,)) or {}
    projects = db.fetch_all("SELECT id FROM contributor_projects WHERE account_id=%s", (account_id,))
    experience = db.fetch_all("SELECT id FROM contributor_experience WHERE account_id=%s", (account_id,))
    education = db.fetch_all("SELECT id FROM contributor_education WHERE account_id=%s", (account_id,))

    # All six sections are tracked for display, but only the four that the
    # profile-completion PAGE can actually fill (expertise, projects, experience,
    # education) gate the dashboard. Basics + skills are edited in profile
    # settings and are surfaced here as informational only — requiring them would
    # make 100% unreachable from this page and lock the dashboard forever.
    sections = {
        "basics": bool(profile.get("bio") and profile.get("country") and profile.get("timezone")),
        "skills": bool(profile.get("primary_skills")),
        "expertise": bool(profile.get("expertise_areas")),
        "projects": len(projects) > 0,
        "experience": len(experience) > 0,
        "education": len(education) > 0,
    }
    GATING = ("expertise", "projects", "experience", "education")
    gating_done = sum(1 for k in GATING if sections[k])
    pct = int(gating_done / len(GATING) * 100)
    complete = gating_done == len(GATING)
    # Persist the flag so the gate can read it cheaply.
    if bool(profile.get("profile_completed")) != complete:
        db.execute("UPDATE contributor_profiles SET profile_completed=%s WHERE account_id=%s",
                   (complete, account_id))
    return {
        "completeness": pct,
        "complete": complete,
        "sections": sections,
        "missing": [k for k in GATING if not sections[k]],
    }


@router.get("/profile/completion")
async def profile_completion(account_id: AcctId):
    return _completion_status(account_id)


# ---- Projects CRUD ----
@router.get("/profile/projects")
async def list_projects(account_id: AcctId):
    return db.fetch_all(
        "SELECT * FROM contributor_projects WHERE account_id=%s ORDER BY created_at DESC", (account_id,))


@router.post("/profile/projects", status_code=201)
async def add_project(account_id: AcctId, body: dict = Body(default={})):
    row = db.execute(
        """INSERT INTO contributor_projects (account_id,title,description,role,url,skills,start_date,end_date)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (account_id, body.get("title", ""), body.get("description", ""), body.get("role"),
         body.get("url"), body.get("skills") or [], body.get("start_date"), body.get("end_date")))
    return row


@router.delete("/profile/projects/{project_id}")
async def delete_project(account_id: AcctId, project_id: int):
    row = db.execute("DELETE FROM contributor_projects WHERE id=%s AND account_id=%s RETURNING id",
                     (project_id, account_id))
    _require(row, "Project")
    return {"ok": True, "deleted_id": project_id}


# ---- Experience / internships CRUD ----
@router.get("/profile/experience")
async def list_experience(account_id: AcctId):
    return db.fetch_all(
        "SELECT * FROM contributor_experience WHERE account_id=%s ORDER BY created_at DESC", (account_id,))


@router.post("/profile/experience", status_code=201)
async def add_experience(account_id: AcctId, body: dict = Body(default={})):
    row = db.execute(
        """INSERT INTO contributor_experience
           (account_id,kind,organization,role,description,location,start_date,end_date,is_current)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (account_id, body.get("kind", "internship"), body.get("organization", ""), body.get("role", ""),
         body.get("description"), body.get("location"), body.get("start_date"),
         body.get("end_date"), bool(body.get("is_current"))))
    return row


@router.delete("/profile/experience/{exp_id}")
async def delete_experience(account_id: AcctId, exp_id: int):
    row = db.execute("DELETE FROM contributor_experience WHERE id=%s AND account_id=%s RETURNING id",
                     (exp_id, account_id))
    _require(row, "Experience")
    return {"ok": True, "deleted_id": exp_id}


# ---- Education CRUD ----
@router.get("/profile/education")
async def list_education(account_id: AcctId):
    return db.fetch_all(
        "SELECT * FROM contributor_education WHERE account_id=%s ORDER BY created_at DESC", (account_id,))


@router.post("/profile/education", status_code=201)
async def add_education(account_id: AcctId, body: dict = Body(default={})):
    row = db.execute(
        """INSERT INTO contributor_education (account_id,institution,degree,field,grade,start_year,end_year)
           VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (account_id, body.get("institution", ""), body.get("degree"), body.get("field"),
         body.get("grade"), body.get("start_year"), body.get("end_year")))
    return row


@router.delete("/profile/education/{edu_id}")
async def delete_education(account_id: AcctId, edu_id: int):
    row = db.execute("DELETE FROM contributor_education WHERE id=%s AND account_id=%s RETURNING id",
                     (edu_id, account_id))
    _require(row, "Education")
    return {"ok": True, "deleted_id": edu_id}


# ---- Expertise areas (stored on profile) ----
@router.patch("/profile/expertise")
async def update_expertise(account_id: AcctId, body: dict = Body(default={})):
    _ensure_profile(account_id)
    db.execute("UPDATE contributor_profiles SET expertise_areas=%s WHERE account_id=%s",
               (body.get("expertise_areas") or [], account_id))
    return _completion_status(account_id)


@router.get("/profile/digital-twin")
async def get_digital_twin(account_id: AcctId):
    snapshot = _build_digital_twin(account_id)
    db.execute("INSERT INTO contributor_digital_twin (account_id, snapshot) VALUES (%s,%s)",
               (account_id, Json(snapshot)))
    return snapshot


@router.get("/profile/digital-twin/history")
async def digital_twin_history(account_id: AcctId, page: int = 1, page_size: int = 20):
    rows = db.fetch_all(
        "SELECT id, snapshot, created_at FROM contributor_digital_twin WHERE account_id=%s ORDER BY created_at DESC",
        (account_id,),
    )
    return db.paginate(rows, page, page_size)


@router.get("/search")
async def contributor_search(account_id: AcctId, q: str = Query(default="")):
    term = f"%{q.lower()}%"
    tasks = db.fetch_all(
        "SELECT id, title, status, category FROM contributor_tasks "
        "WHERE account_id=%s AND LOWER(title) LIKE %s LIMIT 10",
        (account_id, term),
    )
    creds = db.fetch_all(
        "SELECT id, title, status FROM contributor_credentials "
        "WHERE account_id=%s AND LOWER(title) LIKE %s LIMIT 10",
        (account_id, term),
    )
    return {"query": q, "tasks": tasks, "credentials": creds}


# ════════════════════════════════════════════════════════════════════════════
# Track (contributor persona + onboarding status)
# ════════════════════════════════════════════════════════════════════════════

@router.get("/track")
async def get_track(account_id: AcctId):
    """Return contributor track/persona + onboarding completion status.

    Derives from contributor_profiles (existing table) + contributor_kyc.
    Response shape matches the FE ContributorTrackStatus interface.
    """
    acct = db.get_account(account_id) or {}
    prof = db.fetch_one("SELECT * FROM contributor_profiles WHERE account_id=%s", (account_id,)) or {}
    kyc = db.fetch_one("SELECT status FROM contributor_kyc WHERE account_id=%s", (account_id,)) or {}

    contrib_type = prof.get("contrib_type") or prof.get("contribType") or "general_workforce"

    # Derive persona from contrib_type
    _type_to_persona = {
        "internal": "internal",
        "student": "student",
        "women_workforce": "women",
        "general_workforce": "freelancer",
    }
    persona = _type_to_persona.get(contrib_type, "freelancer")

    # Onboarding complete = has bio + country + timezone set
    onboarding_complete = bool(
        prof.get("bio") and prof.get("country") and prof.get("timezone")
    )

    # KYC: general_workforce + women_workforce require KYC
    _requires_kyc = contrib_type in ("general_workforce", "women_workforce")
    kyc_status = kyc.get("status") or "not_started"
    if not _requires_kyc:
        kyc_review_status = "not_required"
    elif kyc_status == "verified":
        kyc_review_status = "approved"
    else:
        kyc_review_status = "pending"

    portal_ready = onboarding_complete and (not _requires_kyc or kyc_status == "verified")

    # Onboarding track
    _track_map = {
        "internal": "internal",
        "student": "student",
        "women_workforce": "women_workforce",
        "general_workforce": "general",
    }
    onboarding_track = _track_map.get(contrib_type, "general")

    profile_out = None
    if prof:
        profile_out = {
            "country": prof.get("country") or "",
            "timezone": prof.get("timezone") or "",
            "degree": None,
            "branch": None,
            "departmentCategory": prof.get("department") or "general",
            "departmentOther": None,
            "availability": str(prof.get("availability") or ""),
            "primarySkills": prof.get("primary_skills") or [],
            "linkedin": prof.get("linkedin"),
        }

    # org chip for internal employees
    org_chip = None
    if persona == "internal" and acct:
        org_chip = {
            "tenant": acct.get("organization") or acct.get("name") or "",
            "department": prof.get("department") or "general",
        }

    return {
        "onboardingComplete": onboarding_complete,
        "portalReady": portal_ready,
        "kycReviewStatus": kyc_review_status,
        "onboardingTrack": onboarding_track,
        "contribType": contrib_type,
        "persona": persona,
        "profile": profile_out,
        "orgChip": org_chip,
        "supervision": None,
        "womenSupport": None,
    }


# ════════════════════════════════════════════════════════════════════════════
# Account-auth (sign-in method summary for Settings)
# ════════════════════════════════════════════════════════════════════════════

@router.get("/account-auth")
async def get_account_auth(account_id: AcctId):
    """Return auth mode info — password vs social OAuth vs enterprise SSO.

    Derives from login_accounts.provider / is_password_set / role columns.
    Response shape matches ContributorAccountAuth in the FE.
    """
    acct = db.get_account(account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="User not found.")

    has_password = bool(acct.get("is_password_set") or acct.get("password_hash"))
    provider = acct.get("provider") or None
    role = acct.get("role") or ""
    tenant_id = acct.get("tenant_id") or acct.get("organization_id") or None

    _social_providers = {"google", "microsoft-entra-id", "microsoft"}

    # Determine auth mode
    if has_password:
        auth_mode = "password"
    elif role == "internal" or (tenant_id and provider not in _social_providers):
        auth_mode = "enterprise_sso"
    else:
        auth_mode = "social_oauth"

    # connected providers list
    connected_providers: list[str] = []
    if provider == "google":
        connected_providers = ["google"]
    elif provider in ("microsoft-entra-id", "microsoft"):
        connected_providers = ["microsoft"]

    # organization name
    org_name: str | None = None
    if tenant_id:
        try:
            org_row = db.fetch_one("SELECT name FROM organizations WHERE id=%s", (tenant_id,))
            org_name = (org_row or {}).get("name")
        except Exception:  # noqa: BLE001
            pass  # organizations table may not exist — org_name stays None

    return {
        "authMode": auth_mode,
        "hasPassword": has_password,
        "provider": provider,
        "connectedProviders": connected_providers,
        "organizationName": org_name,
    }


# ════════════════════════════════════════════════════════════════════════════
# Mentorship opt-in + status
# ════════════════════════════════════════════════════════════════════════════

@router.post("/mentorship/opt-in", status_code=201)
async def mentorship_opt_in(account_id: AcctId, payload: dict = Body(default={})):
    """Opt the contributor into the mentorship programme. Idempotent — if
    already opted in, updates focus and returns the existing row.

    Persists to contributor_mentorship and writes an audit event.
    """
    focus = str(payload.get("focus") or "").strip()[:500] or None

    row = db.execute(
        """
        INSERT INTO contributor_mentorship (account_id, focus, status, opted_in_at, data)
        VALUES (%s, %s, 'pending', now(), %s)
        ON CONFLICT (account_id) DO UPDATE
            SET focus      = EXCLUDED.focus,
                updated_at = now(),
                data       = contributor_mentorship.data || EXCLUDED.data
        RETURNING *
        """,
        (account_id, focus, Json({"source": "contributor_self"})),
    )

    write_audit(
        actor_id=str(account_id), actor_email=None, actor_role="contributor",
        action="mentorship.opt_in", service="contributor-service",
        target_id=str(row["id"]),
        extra={"focus": focus},
    )

    session_like = {
        "id": str(row["id"]),
        "contributorAccountId": str(account_id),
        "mentorId": row.get("mentor_id"),
        "status": row.get("status") or "pending",
        "focus": row.get("focus"),
        "createdAt": row.get("opted_in_at"),
    }
    return {"session": session_like, "assigned": bool(row.get("mentor_id"))}


@router.get("/mentorship/status")
async def mentorship_status(account_id: AcctId):
    """Return the contributor's mentorship opt-in status and upcoming session info."""
    row = db.fetch_one(
        "SELECT * FROM contributor_mentorship WHERE account_id=%s", (account_id,)
    )
    opted_in = row is not None and row.get("opted_in_at") is not None

    upcoming_session = None
    if row and row.get("mentor_id") and row.get("status") in ("assigned", "active", "pending"):
        upcoming_session = {
            "id": str(row["id"]),
            "mentorId": row.get("mentor_id"),
            "status": row.get("status"),
            "scheduledAt": None,  # Populated when mentor sets a time
        }

    return {
        "optedIn": opted_in,
        "focus": (row or {}).get("focus"),
        "optedInAt": (row or {}).get("opted_in_at"),
        "upcomingSession": upcoming_session,
    }


# ════════════════════════════════════════════════════════════════════════════
# Account deactivate
# ════════════════════════════════════════════════════════════════════════════

@router.post("/account/deactivate")
async def deactivate_account(account_id: AcctId, payload: dict = Body(default={})):
    db.execute("UPDATE login_accounts SET is_active=FALSE, updated_at=now() WHERE id=%s", (account_id,))
    write_audit(actor_id=str(account_id), actor_email=None, actor_role="contributor",
                action="account_deactivated", service="contributor-service",
                details=payload.get("reason"))
    publish_event("contributor.account_deactivated", {"accountId": str(account_id)})
    return {"ok": True, "is_active": False}


# ════════════════════════════════════════════════════════════════════════════
# Public credential share (NO auth)
# ════════════════════════════════════════════════════════════════════════════

@public_router.get("/credentials/{share_id}")
async def public_credential(share_id: str):
    share = db.fetch_one("SELECT * FROM credential_shares WHERE share_id=%s", (share_id,))
    if share is None:
        raise HTTPException(status_code=404, detail="Shared credential not found")
    cred = db.fetch_one("SELECT * FROM contributor_credentials WHERE id=%s", (share["credential_id"],))
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential no longer available")
    acct = db.get_account(share["account_id"]) or {}
    return {
        "share_id": share_id,
        "title": cred.get("title"),
        "issuer": cred.get("issuer"),
        "kind": cred.get("kind"),
        "status": cred.get("status"),
        "verification_code": cred.get("verification_code"),
        "issued_at": cred.get("issued_at"),
        "holder": (acct.get("name") or "").strip() or "Glimmora Contributor",
    }
