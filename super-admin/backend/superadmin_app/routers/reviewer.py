"""
Reviewer-token endpoints the frontend reviewer pages call.

  GET   /api/v1/reviewer/dashboard
  GET   /api/v1/reviewer/projects
  PATCH /api/v1/reviewer/assignments/{id}
  POST  /api/v1/reviewer/evidence/{id}/recommend

All are protected with get_current_user and require role reviewer (admins allowed).
Assignments + recommendations are persisted in reviewer_assignments /
reviewer_recommendations.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from shared.audit import write_audit
from shared.deps import get_current_user
from shared.kafka_bus import publish_event

from superadmin_app import repo

router = APIRouter(prefix="/api/v1/reviewer", tags=["reviewer"])

_REVIEWER_ROLES = {"reviewer", "admin", "superadmin", "super_admin"}


def _require_reviewer(user: dict) -> dict:
    if (user.get("role") or "").lower() not in _REVIEWER_ROLES:
        raise HTTPException(status_code=403, detail="Reviewer access required")
    return user


# ── GET /dashboard ────────────────────────────────────────────────────────────

@router.get("/dashboard")
async def reviewer_dashboard(user: Annotated[dict, Depends(get_current_user)]):
    _require_reviewer(user)
    counts = repo.reviewer_assignment_counts(user.get("id"), user.get("email"))
    assignments = repo.list_assignments_for_reviewer(user.get("id"), user.get("email"))
    return {
        "reviewer": {"id": user.get("id"), "email": user.get("email"), "role": user.get("role")},
        "stats": {
            "total": counts.get("total", 0),
            "pending": counts.get("pending", 0),
            "inReview": counts.get("in_review", 0),
            "approved": counts.get("approved", 0),
            "rejected": counts.get("rejected", 0),
            "completed": counts.get("completed", 0),
        },
        "assignments": assignments,
    }


# ── GET /assigned-sows ────────────────────────────────────────────────────────

@router.get("/assigned-sows")
async def reviewer_assigned_sows(user: Annotated[dict, Depends(get_current_user)]):
    """SOWs this reviewer was assigned to at intake (admin_records kind=sow_reviewer).
    Shown in the reviewer portal immediately — BEFORE any delivery — so the
    reviewer can see what they're responsible for. Delivered tasks for QA still
    flow through reviewer_assignments separately."""
    _require_reviewer(user)
    import json as _json
    from shared.db import get_pg_connection
    from psycopg2.extras import RealDictCursor

    rid = str(user.get("id") or "")
    email = (user.get("email") or "").lower()
    conn = get_pg_connection()
    sows = []
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Match the reviewer by id OR email inside the JSONB payload.
        cur.execute(
            """
            SELECT ar.name AS sow_id, ar.status AS assign_status, ar.data AS assign_data,
                   ar.updated_at AS assigned_at, s.data AS sow_data, s.owner_email
            FROM admin_records ar
            LEFT JOIN enterprise_sows s ON s.id = ar.name
            WHERE ar.kind = 'sow_reviewer' AND ar.deleted_at IS NULL
              AND (ar.data->>'reviewerId' = %s OR lower(ar.data->>'reviewerEmail') = %s)
            ORDER BY ar.updated_at DESC
            """,
            (rid, email),
        )
        for r in cur.fetchall():
            ad = r["assign_data"]
            if isinstance(ad, str):
                try:
                    ad = _json.loads(ad)
                except (ValueError, TypeError):
                    ad = {}
            sd = r["sow_data"] or {}
            if isinstance(sd, str):
                try:
                    sd = _json.loads(sd)
                except (ValueError, TypeError):
                    sd = {}
            title = None
            if isinstance(sd, dict):
                title = sd.get("projectTitle") or sd.get("title") or sd.get("fileName")
            sows.append({
                "sowId": r["sow_id"],
                "title": title or r["sow_id"],
                "status": sd.get("status") if isinstance(sd, dict) else None,
                "stage": sd.get("status") if isinstance(sd, dict) else None,
                "ownerEmail": r.get("owner_email"),
                "assignmentStatus": r["assign_status"],
                "assignedAt": r["assigned_at"].isoformat() if r.get("assigned_at") else None,
            })
    return {"sows": sows, "total": len(sows)}


# ── GET /projects ─────────────────────────────────────────────────────────────

@router.get("/projects")
async def reviewer_projects(user: Annotated[dict, Depends(get_current_user)]):
    _require_reviewer(user)
    assignments = repo.list_assignments_for_reviewer(user.get("id"), user.get("email"))
    # Group assignments by project so the reviewer page can render project cards.
    projects: dict[str, dict[str, Any]] = {}
    for a in assignments:
        pid = a.get("projectId") or a["id"]
        proj = projects.setdefault(pid, {
            "projectId": pid,
            "projectName": a.get("projectName") or a.get("title") or "Untitled",
            "assignments": [],
        })
        proj["assignments"].append(a)
    return {"projects": list(projects.values()), "total": len(assignments)}


# ── PATCH /assignments/{id} ───────────────────────────────────────────────────

class AssignmentPatch(BaseModel):
    status: str | None = None
    priority: str | None = None
    data: dict[str, Any] | None = None


@router.patch("/assignments/{assignment_id}")
async def patch_assignment(
    assignment_id: str,
    body: AssignmentPatch,
    request: Request,
    user: Annotated[dict, Depends(get_current_user)],
):
    _require_reviewer(user)
    existing = repo.get_assignment(assignment_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Assignment not found")

    # Claim a pool assignment to this reviewer when they act on it.
    claim_data = dict(body.data or {})
    updated = repo.update_assignment(
        assignment_id, status=body.status, priority=body.priority, data=body.data,
        reviewer_id=user.get("id"), reviewer_email=user.get("email"),
    )

    # When the enterprise reviewer approves/completes, mark the underlying
    # contributor submission + task accepted — the final stage of two-stage review.
    if (body.status or "").lower() in ("approved", "completed", "accepted"):
        ex_data = (existing.get("data") or {}) if isinstance(existing, dict) else {}
        if isinstance(ex_data, str):
            import json as _json
            try:
                ex_data = _json.loads(ex_data)
            except (ValueError, TypeError):
                ex_data = {}
        sub_id = ex_data.get("submissionId") or claim_data.get("submissionId")
        task_id = ex_data.get("taskId") or claim_data.get("taskId")
        acct_id = ex_data.get("accountId") or claim_data.get("accountId")
        try:
            from shared.db import get_pg_connection
            conn = get_pg_connection()
            with conn.cursor() as cur:
                if sub_id:
                    cur.execute("UPDATE contributor_submissions SET status='accepted', updated_at=now() WHERE id=%s", (sub_id,))
                if task_id and acct_id:
                    cur.execute("UPDATE contributor_tasks SET status='completed', updated_at=now() WHERE id=%s AND account_id=%s", (task_id, acct_id))
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
    publish_event("reviewer.assignment_updated",
                  {"assignmentId": assignment_id, "status": body.status,
                   "reviewerId": user.get("id")})
    write_audit(actor_id=user.get("id"), actor_email=user.get("email"), actor_role=user.get("role"),
                action="update_assignment", target_id=assignment_id, service="superadmin-service",
                ip_address=request.client.host if request.client else None,
                extra={"status": body.status})
    return {"assignment": updated}


# ── POST /evidence/{id}/recommend ─────────────────────────────────────────────

class RecommendRequest(BaseModel):
    recommendation: str | None = None
    score: float | None = None
    comment: str | None = None
    assignmentId: str | None = None
    data: dict[str, Any] | None = None


@router.post("/evidence/{evidence_id}/recommend", status_code=201)
async def recommend_evidence(
    evidence_id: str,
    body: RecommendRequest,
    request: Request,
    user: Annotated[dict, Depends(get_current_user)],
):
    _require_reviewer(user)
    reco = repo.create_recommendation(
        evidence_id=evidence_id,
        assignment_id=body.assignmentId,
        reviewer_id=user.get("id"),
        reviewer_email=user.get("email"),
        recommendation=body.recommendation,
        score=body.score,
        comment=body.comment,
        data=body.data,
    )
    publish_event("reviewer.evidence_recommended",
                  {"evidenceId": evidence_id, "recommendation": body.recommendation,
                   "reviewerId": user.get("id")})
    write_audit(actor_id=user.get("id"), actor_email=user.get("email"), actor_role=user.get("role"),
                action="recommend_evidence", target=evidence_id, target_id=reco.get("id"),
                service="superadmin-service",
                ip_address=request.client.host if request.client else None,
                extra={"recommendation": body.recommendation})
    return {"recommendation": reco}
