"""
Mentor router — /api/mentor/**

All endpoints Bearer-protected; allowed roles: mentor | reviewer | admin
(+ superadmin). Real persistence to Postgres; nested data stored as JSONB.
Demo rows are seeded on the mentor's first authenticated access.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2.extras import Json, RealDictCursor

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import require_roles

from mentor_app.schemas import (
    DecisionRequest,
    EscalationCreateRequest,
    EscalationUpdateRequest,
    MenteeNoteRequest,
    ProfileUpdateRequest,
    SettingsUpdateRequest,
)
from mentor_app.seed import ensure_profile, seed_mentor_demo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mentor", tags=["mentor"])

# Roles allowed on every mentor endpoint.
mentor_user = require_roles("mentor", "reviewer", "admin", "superadmin")
MentorDep = Annotated[dict, Depends(mentor_user)]

# Decision → resulting review status mapping.
_DECISION_STATUS = {"accept": "accepted", "rework": "rework", "escalate": "escalated"}


# ── helpers ──────────────────────────────────────────────────────────────────

def _ok(data: Any) -> dict:
    return {"success": True, "message": None, "data": data}


def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def _ensure_seeded(user: dict) -> None:
    """No-op. Demo seeding is DISABLED — mentors must see only their own real
    data (reviews/mentorships scoped to their account id). Each mentor starts
    with an empty workspace until real submissions are routed to them. Kept as a
    no-op so the many call sites don't need touching; seed_mentor_demo stays
    importable for one-off local fixtures only."""
    _ = (user, seed_mentor_demo)  # referenced to avoid unused-import churn


def _serialize(row: dict) -> dict:
    """Make a RealDict row JSON-safe (datetimes → iso)."""
    out = dict(row)
    for k, v in out.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


# ── dashboard ──────────────────────────────────────────────────────────────

@router.get("/dashboard")
def dashboard(user: MentorDep):
    _ensure_seeded(user)
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE status IN ('pending','in_review')) AS pending_reviews,
              COUNT(*) FILTER (WHERE status IN ('accepted','rework')) AS completed_reviews,
              COUNT(*) AS total_reviews
            FROM mentor_reviews WHERE mentor_id = %s
            """,
            (mentor_id,),
        )
        rstats = cur.fetchone() or {}
        cur.execute(
            "SELECT COUNT(*) AS mentees FROM mentor_mentorships "
            "WHERE mentor_id = %s AND status != 'completed'",
            (mentor_id,),
        )
        mentees = (cur.fetchone() or {}).get("mentees", 0)
        cur.execute(
            "SELECT COUNT(*) AS open_escalations FROM mentor_escalations "
            "WHERE mentor_id = %s AND status IN ('open','in_progress')",
            (mentor_id,),
        )
        escalations = (cur.fetchone() or {}).get("open_escalations", 0)

        cur.execute(
            """
            SELECT id, title, submission_type, contributor_name, priority, status,
                   created_at
            FROM mentor_reviews
            WHERE mentor_id = %s AND status IN ('pending','in_review')
            ORDER BY created_at DESC LIMIT 5
            """,
            (mentor_id,),
        )
        recent = [_serialize(r) for r in cur.fetchall()]

    return _ok({
        "stats": {
            "pending_reviews": int(rstats.get("pending_reviews") or 0),
            "completed_reviews": int(rstats.get("completed_reviews") or 0),
            "total_reviews": int(rstats.get("total_reviews") or 0),
            "mentees": int(mentees or 0),
            "escalations": int(escalations or 0),
        },
        "recent_queue": recent,
    })


# ── assigned SOWs (mapped from admin_records.sow_mentor) ──────────────────────

@router.get("/assigned-sows")
def assigned_sows(user: MentorDep):
    """SOWs this mentor was assigned to at the Commercial gate. Maps:
        admin_records (kind=sow_mentor, data.mentorId/email = this mentor)
          → enterprise_sows (by id)
    Shown immediately in the mentor portal so the assigned SOW is visible even
    before any contributor submits work. All joins are by UNIQUE id."""
    import json as _json
    mentor_id = str(user.get("id") or "")
    email = (user.get("email") or "").lower()
    conn = _conn()
    out = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT ar.name AS sow_id, ar.status AS assign_status, ar.data AS assign_data,
                   ar.updated_at AS assigned_at, s.data AS sow_data, s.owner_email
            FROM admin_records ar
            LEFT JOIN enterprise_sows s ON s.id = ar.name
            WHERE ar.kind = 'sow_mentor' AND ar.deleted_at IS NULL
              AND (ar.data->>'mentorId' = %s OR lower(ar.data->>'mentorEmail') = %s)
            ORDER BY ar.updated_at DESC
            """,
            (mentor_id, email),
        )
        for r in cur.fetchall():
            sd = r["sow_data"] or {}
            if isinstance(sd, str):
                try:
                    sd = _json.loads(sd)
                except (ValueError, TypeError):
                    sd = {}
            title = None
            if isinstance(sd, dict):
                title = sd.get("projectTitle") or sd.get("title") or sd.get("fileName")
            out.append({
                "sowId": r["sow_id"],
                "title": title or r["sow_id"],
                "status": sd.get("status") if isinstance(sd, dict) else None,
                "stage": sd.get("status") if isinstance(sd, dict) else None,
                "ownerEmail": r.get("owner_email"),
                "assignmentStatus": r["assign_status"],
                "assignedAt": r["assigned_at"].isoformat() if r.get("assigned_at") else None,
            })
    return _ok({"sows": out, "total": len(out)})


# ── review queue ─────────────────────────────────────────────────────────────

@router.get("/queue")
def list_queue(
    user: MentorDep,
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    _ensure_seeded(user)
    mentor_id = str(user["id"])
    # Include unclaimed 'pool' reviews (real contributor submissions land here)
    # alongside this mentor's own assigned reviews.
    where = ["(mentor_id = %s OR mentor_id = 'pool')"]
    params: list[Any] = [mentor_id]
    if status:
        where.append("status = %s")
        params.append(status)
    else:
        # default queue = open items
        where.append("status IN ('pending','in_review','escalated')")
    if priority:
        where.append("priority = %s")
        params.append(priority)
    if q:
        where.append("(title ILIKE %s OR contributor_name ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    clause = " AND ".join(where)
    offset = (page - 1) * page_size

    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM mentor_reviews WHERE {clause}", params)
        total = (cur.fetchone() or {}).get("total", 0)
        cur.execute(
            f"""
            SELECT id, title, submission_type, contributor_name, priority, status,
                   decision, score, mentee_id, created_at, updated_at
            FROM mentor_reviews WHERE {clause}
            ORDER BY
              CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                            WHEN 'normal' THEN 2 ELSE 3 END,
              created_at DESC
            LIMIT %s OFFSET %s
            """,
            params + [page_size, offset],
        )
        items = [_serialize(r) for r in cur.fetchall()]

    return _ok({
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": int(total or 0),
    })


@router.get("/sow/{sow_id}/tasks")
def get_sow_tasks(sow_id: str, user: MentorDep):
    """Decomposed tasks (+ status/assignee) for a SOW — shown to the mentor in the
    review detail. PAYOUT/cost fields are intentionally stripped: mentors see the
    work and its state, never the money."""
    conn = _conn()
    # Find the decomposition plan(s) for this SOW (enterprise_plans.data.sowId).
    tasks: list[dict] = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT data FROM enterprise_plans WHERE data->>'sowId' = %s ORDER BY created_at DESC",
            (sow_id,),
        )
        for row in cur.fetchall():
            data = row["data"] or {}
            for t in (data.get("tasks") or []):
                detail = t.get("detail") or {}
                tasks.append({
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "milestone": t.get("milestone") or t.get("milestoneId"),
                    "status": t.get("status") or "published",
                    "assignee": t.get("assigneeEmail") or t.get("assigneeId") or "Unassigned",
                    "effortHours": t.get("effortHours") or detail.get("effortHours"),
                    "skills": t.get("requiredSkills") or detail.get("requiredSkills") or [],
                    # NOTE: deliberately NO priceMinor / payout / cost fields.
                })
    return _ok({"sowId": sow_id, "tasks": tasks, "total": len(tasks)})


@router.get("/queue/{review_id}")
def get_queue_item(review_id: int, user: MentorDep):
    _ensure_seeded(user)
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM mentor_reviews WHERE id = %s AND (mentor_id = %s OR mentor_id = 'pool')",
            (review_id, mentor_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Review not found")
        notes = []
        cur.execute(
            "SELECT id, body, attachments, created_at FROM mentor_notes "
            "WHERE review_id = %s ORDER BY created_at DESC",
            (review_id,),
        )
        notes = [_serialize(n) for n in cur.fetchall()]
    out = _serialize(row)
    out["notes"] = notes
    return _ok(out)


@router.post("/queue/{review_id}/decision")
def submit_decision(review_id: int, body: DecisionRequest, user: MentorDep):
    mentor_id = str(user["id"])
    new_status = _DECISION_STATUS[body.decision]
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, mentee_id, title, contributor_name, payload FROM mentor_reviews "
            "WHERE id = %s AND (mentor_id = %s OR mentor_id = 'pool')",
            (review_id, mentor_id),
        )
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Review not found")

        # Claim the review to this mentor (pool → mine) as part of deciding it.
        cur.execute(
            """
            UPDATE mentor_reviews
               SET decision = %s, status = %s, score = %s, comments = %s,
                   mentor_id = %s, mentor_email = %s,
                   decided_at = now(), updated_at = now()
             WHERE id = %s
            RETURNING *
            """,
            (body.decision, new_status, body.score, body.comments, mentor_id, user.get("email"), review_id),
        )
        updated = cur.fetchone()

        # Two-stage hand-off: when the mentor ACCEPTS, route the submission to the
        # enterprise reviewer as a pending reviewer_assignment (reviewer_id NULL =
        # pool, claimed when a reviewer opens it). Rework/escalate stay with mentor.
        if body.decision == "accept":
            pl = existing.get("payload") or {}
            if isinstance(pl, str):
                try:
                    import json as _json
                    pl = _json.loads(pl)
                except (ValueError, TypeError):
                    pl = {}
            try:
                cur.execute(
                    """
                    INSERT INTO reviewer_assignments
                        (reviewer_id, reviewer_email, project_id, project_name, title,
                         status, priority, data)
                    VALUES (NULL, NULL, %s, %s, %s, 'pending', 'normal', %s)
                    """,
                    (str(pl.get("taskId") or ""), existing.get("title"),
                     f"Review: {existing.get('title')}",
                     Json({"stage": "enterprise_reviewer", "fromMentorReview": review_id,
                           "taskId": pl.get("taskId"), "submissionId": pl.get("submissionId"),
                           "accountId": pl.get("accountId"), "contributorName": existing.get("contributor_name"),
                           "url": pl.get("url"), "summary": pl.get("summary")})),
                )
            except Exception:  # noqa: BLE001 — reviewer table lives in shared DB
                pass

        # A decision optionally records a review note.
        if body.comments:
            cur.execute(
                "INSERT INTO mentor_notes (mentor_id, tenant_id, mentee_id, review_id, "
                "kind, body) VALUES (%s,%s,%s,%s,'review',%s)",
                (mentor_id, user.get("tenant_id"), existing.get("mentee_id"),
                 review_id, body.comments),
            )

        # Escalate decision auto-creates an escalation row.
        if body.decision == "escalate":
            cur.execute(
                """
                INSERT INTO mentor_escalations
                    (mentor_id, mentor_email, tenant_id, review_id, mentee_id, subject,
                     category, priority, status, description, timeline)
                VALUES (%s,%s,%s,%s,%s,%s,'quality','high','open',%s,%s)
                """,
                (
                    mentor_id, user.get("email"), user.get("tenant_id"), review_id,
                    existing.get("mentee_id"),
                    f"Escalation: {existing.get('title')}",
                    body.comments or "Escalated from review queue.",
                    Json([{"at": datetime.now(timezone.utc).isoformat(), "status": "open",
                           "by": user.get("email"), "note": "Auto-created from review decision"}]),
                ),
            )
    conn.commit()

    write_audit(
        actor_id=mentor_id, actor_email=user.get("email"), actor_role=user.get("role"),
        action="mentor.review.decision", target="mentor_reviews", target_id=str(review_id),
        details=body.decision, service="mentor-service", tenant_id=user.get("tenant_id"),
        extra={"score": body.score},
    )
    return _ok(_serialize(updated))


# ── mentorship ───────────────────────────────────────────────────────────────

@router.get("/mentorship")
def list_mentees(
    user: MentorDep,
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    _ensure_seeded(user)
    mentor_id = str(user["id"])
    where = ["mentor_id = %s"]
    params: list[Any] = [mentor_id]
    if status:
        where.append("status = %s")
        params.append(status)
    clause = " AND ".join(where)
    offset = (page - 1) * page_size
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM mentor_mentorships WHERE {clause}", params)
        total = (cur.fetchone() or {}).get("total", 0)
        cur.execute(
            f"""
            SELECT id, mentee_name, mentee_email, role, track, status, progress,
                   updated_at
            FROM mentor_mentorships WHERE {clause}
            ORDER BY updated_at DESC LIMIT %s OFFSET %s
            """,
            params + [page_size, offset],
        )
        items = [_serialize(r) for r in cur.fetchall()]
    return _ok({"items": items, "page": page, "page_size": page_size, "total": int(total or 0)})


@router.get("/mentorship/{mentee_id}")
def get_mentee(mentee_id: int, user: MentorDep):
    _ensure_seeded(user)
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM mentor_mentorships WHERE id = %s AND mentor_id = %s",
            (mentee_id, mentor_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Mentee not found")
        cur.execute(
            "SELECT id, body, attachments, kind, created_at FROM mentor_notes "
            "WHERE mentee_id = %s AND mentor_id = %s ORDER BY created_at DESC",
            (mentee_id, mentor_id),
        )
        notes = [_serialize(n) for n in cur.fetchall()]
    out = _serialize(row)
    out["notes"] = notes
    return _ok(out)


@router.post("/mentorship/{mentee_id}/note")
def add_mentee_note(mentee_id: int, body: MenteeNoteRequest, user: MentorDep):
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id FROM mentor_mentorships WHERE id = %s AND mentor_id = %s",
            (mentee_id, mentor_id),
        )
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Mentee not found")
        cur.execute(
            """
            INSERT INTO mentor_notes (mentor_id, tenant_id, mentee_id, kind, body, attachments)
            VALUES (%s,%s,%s,'mentorship',%s,%s)
            RETURNING id, body, attachments, kind, created_at
            """,
            (mentor_id, user.get("tenant_id"), mentee_id, body.body, Json(body.attachments)),
        )
        note = cur.fetchone()
        cur.execute("UPDATE mentor_mentorships SET updated_at = now() WHERE id = %s", (mentee_id,))
    conn.commit()
    write_audit(
        actor_id=mentor_id, actor_email=user.get("email"), actor_role=user.get("role"),
        action="mentor.mentorship.note", target="mentor_mentorships", target_id=str(mentee_id),
        service="mentor-service", tenant_id=user.get("tenant_id"),
    )
    return _ok(_serialize(note))


# ── escalation ───────────────────────────────────────────────────────────────

@router.get("/escalation")
def list_escalations(
    user: MentorDep,
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    _ensure_seeded(user)
    mentor_id = str(user["id"])
    where = ["mentor_id = %s"]
    params: list[Any] = [mentor_id]
    if status:
        where.append("status = %s")
        params.append(status)
    if priority:
        where.append("priority = %s")
        params.append(priority)
    clause = " AND ".join(where)
    offset = (page - 1) * page_size
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM mentor_escalations WHERE {clause}", params)
        total = (cur.fetchone() or {}).get("total", 0)
        cur.execute(
            f"""
            SELECT id, subject, category, priority, status, review_id, mentee_id,
                   assignee, created_at, updated_at, resolved_at
            FROM mentor_escalations WHERE {clause}
            ORDER BY created_at DESC LIMIT %s OFFSET %s
            """,
            params + [page_size, offset],
        )
        items = [_serialize(r) for r in cur.fetchall()]
    return _ok({"items": items, "page": page, "page_size": page_size, "total": int(total or 0)})


@router.post("/escalation")
def create_escalation(body: EscalationCreateRequest, user: MentorDep):
    mentor_id = str(user["id"])
    conn = _conn()
    now_iso = datetime.now(timezone.utc).isoformat()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO mentor_escalations
                (mentor_id, mentor_email, tenant_id, review_id, mentee_id, subject,
                 category, priority, status, description, timeline, meta)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s)
            RETURNING *
            """,
            (
                mentor_id, user.get("email"), user.get("tenant_id"), body.review_id,
                body.mentee_id, body.subject, body.category, body.priority, body.description,
                Json([{"at": now_iso, "status": "open", "by": user.get("email"),
                       "note": "Escalation created"}]),
                Json(body.meta),
            ),
        )
        created = cur.fetchone()
    conn.commit()
    write_audit(
        actor_id=mentor_id, actor_email=user.get("email"), actor_role=user.get("role"),
        action="mentor.escalation.create", target="mentor_escalations",
        target_id=str(created["id"]), service="mentor-service", tenant_id=user.get("tenant_id"),
    )
    return _ok(_serialize(created))


@router.get("/escalation/{escalation_id}")
def get_escalation(escalation_id: int, user: MentorDep):
    _ensure_seeded(user)
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM mentor_escalations WHERE id = %s AND mentor_id = %s",
            (escalation_id, mentor_id),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Escalation not found")
    return _ok(_serialize(row))


@router.patch("/escalation/{escalation_id}")
def update_escalation(escalation_id: int, body: EscalationUpdateRequest, user: MentorDep):
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM mentor_escalations WHERE id = %s AND mentor_id = %s",
            (escalation_id, mentor_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Escalation not found")

        sets = ["updated_at = now()"]
        params: list[Any] = []
        if body.status is not None:
            sets.append("status = %s")
            params.append(body.status)
            if body.status in ("resolved", "closed"):
                sets.append("resolved_at = now()")
        if body.priority is not None:
            sets.append("priority = %s")
            params.append(body.priority)
        if body.assignee is not None:
            sets.append("assignee = %s")
            params.append(body.assignee)

        # append to JSONB timeline trail
        timeline = row.get("timeline") or []
        timeline.append({
            "at": datetime.now(timezone.utc).isoformat(),
            "status": body.status or row.get("status"),
            "by": user.get("email"),
            "note": body.note or "Updated",
        })
        sets.append("timeline = %s")
        params.append(Json(timeline))

        params.extend([escalation_id, mentor_id])
        cur.execute(
            f"UPDATE mentor_escalations SET {', '.join(sets)} "
            f"WHERE id = %s AND mentor_id = %s RETURNING *",
            params,
        )
        updated = cur.fetchone()
    conn.commit()
    write_audit(
        actor_id=mentor_id, actor_email=user.get("email"), actor_role=user.get("role"),
        action="mentor.escalation.update", target="mentor_escalations",
        target_id=str(escalation_id), details=body.status, service="mentor-service",
        tenant_id=user.get("tenant_id"),
    )
    return _ok(_serialize(updated))


# ── history ──────────────────────────────────────────────────────────────────

@router.get("/history")
def review_history(
    user: MentorDep,
    decision: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    _ensure_seeded(user)
    mentor_id = str(user["id"])
    where = ["mentor_id = %s", "status IN ('accepted','rework','escalated')"]
    params: list[Any] = [mentor_id]
    if decision:
        where.append("decision = %s")
        params.append(decision)
    if q:
        where.append("(title ILIKE %s OR contributor_name ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    clause = " AND ".join(where)
    offset = (page - 1) * page_size
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM mentor_reviews WHERE {clause}", params)
        total = (cur.fetchone() or {}).get("total", 0)
        cur.execute(
            f"""
            SELECT id, title, submission_type, contributor_name, decision, score,
                   comments, status, decided_at, created_at
            FROM mentor_reviews WHERE {clause}
            ORDER BY decided_at DESC NULLS LAST, created_at DESC
            LIMIT %s OFFSET %s
            """,
            params + [page_size, offset],
        )
        items = [_serialize(r) for r in cur.fetchall()]
    return _ok({"items": items, "page": page, "page_size": page_size, "total": int(total or 0)})


# ── profile ──────────────────────────────────────────────────────────────────

@router.get("/profile")
def get_profile(user: MentorDep):
    _ensure_seeded(user)
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        ensure_profile(cur, user)
        conn.commit()
        cur.execute("SELECT * FROM mentor_profiles WHERE mentor_id = %s", (mentor_id,))
        row = cur.fetchone()
    out = _serialize(row) if row else {}
    out["email"] = user.get("email")
    out["role"] = user.get("role")
    # don't leak internal seed sentinel
    if isinstance(out.get("settings"), dict):
        out["settings"].pop("_demo_seeded", None)
    return _ok(out)


@router.patch("/profile")
def update_profile(body: ProfileUpdateRequest, user: MentorDep):
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        ensure_profile(cur, user)
        sets = ["updated_at = now()"]
        params: list[Any] = []
        mapping = {
            "display_name": body.display_name,
            "headline": body.headline,
            "bio": body.bio,
            "timezone": body.timezone,
            "avatar_url": body.avatar_url,
        }
        for col, val in mapping.items():
            if val is not None:
                sets.append(f"{col} = %s")
                params.append(val)
        if body.expertise is not None:
            sets.append("expertise = %s")
            params.append(Json(body.expertise))
        if body.languages is not None:
            sets.append("languages = %s")
            params.append(Json(body.languages))
        if body.links is not None:
            sets.append("links = %s")
            params.append(Json(body.links))
        params.append(mentor_id)
        cur.execute(
            f"UPDATE mentor_profiles SET {', '.join(sets)} WHERE mentor_id = %s RETURNING *",
            params,
        )
        row = cur.fetchone()
    conn.commit()
    out = _serialize(row)
    if isinstance(out.get("settings"), dict):
        out["settings"].pop("_demo_seeded", None)
    write_audit(
        actor_id=mentor_id, actor_email=user.get("email"), actor_role=user.get("role"),
        action="mentor.profile.update", target="mentor_profiles", target_id=mentor_id,
        service="mentor-service", tenant_id=user.get("tenant_id"),
    )
    return _ok(out)


# ── settings ─────────────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings(user: MentorDep):
    _ensure_seeded(user)
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        ensure_profile(cur, user)
        conn.commit()
        cur.execute("SELECT settings FROM mentor_profiles WHERE mentor_id = %s", (mentor_id,))
        row = cur.fetchone()
    settings = (row or {}).get("settings") or {}
    if isinstance(settings, dict):
        settings.pop("_demo_seeded", None)
    return _ok({"settings": settings})


@router.patch("/settings")
def update_settings(body: SettingsUpdateRequest, user: MentorDep):
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        ensure_profile(cur, user)
        cur.execute("SELECT settings FROM mentor_profiles WHERE mentor_id = %s", (mentor_id,))
        row = cur.fetchone()
        current = (row or {}).get("settings") or {}
        if not isinstance(current, dict):
            current = {}
        seeded = current.get("_demo_seeded")
        merged = {**current, **body.settings}
        if seeded is not None:
            merged["_demo_seeded"] = seeded
        cur.execute(
            "UPDATE mentor_profiles SET settings = %s, updated_at = now() "
            "WHERE mentor_id = %s RETURNING settings",
            (Json(merged), mentor_id),
        )
        out = cur.fetchone()
    conn.commit()
    settings = (out or {}).get("settings") or {}
    if isinstance(settings, dict):
        settings.pop("_demo_seeded", None)
    write_audit(
        actor_id=mentor_id, actor_email=user.get("email"), actor_role=user.get("role"),
        action="mentor.settings.update", target="mentor_profiles", target_id=mentor_id,
        service="mentor-service", tenant_id=user.get("tenant_id"),
    )
    return _ok({"settings": settings})
