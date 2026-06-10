"""Portfolio + projects.

Unlike the rest of the service, /api/v1/portfolio and /api/v1/projects return
the contract shape DIRECTLY (no {success,message,data} envelope).

The /api/v1/projects/{id} endpoint returns the stored project data mapped to the
ProjectDetail shape expected by the enterprise frontend:

  ProjectSummary fields: id, name, sponsor, pmo, startedAt, dueAt, progress,
    health, completedAt, sowId, planId
  ProjectDetail extras: slaAtRiskCount, qualityAcceptanceRate, budgetBurnMinor,
    budgetTotalMinor, milestones, recentActivity, aiSignals, contributors,
    reviewers, openExceptions, resolvedExceptions, budget, tasks, audit

Fields missing from stored data are defaulted to safe empty values so the
frontend's TypeScript types are satisfied.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException

from shared.audit import write_audit
from shared.deps import get_current_user

from enterprise_app import db
from enterprise_app.seed import ensure_demo_data

portfolio_router = APIRouter(prefix="/api/v1/portfolio", tags=["portfolio"])
projects_router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


# ── helpers ────────────────────────────────────────────────────────────────────

def _load_project(project_id: str, owner_id: str) -> dict:
    row = db.get_row("project", project_id, owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return row


def _default_budget(data: dict) -> dict:
    """Return a ProjectBudget-shaped dict from stored data, filling defaults."""
    stored = data.get("budget") or {}
    return {
        "budgetMinor": stored.get("budgetMinor", 0),
        "committedMinor": stored.get("committedMinor", 0),
        "paidMinor": stored.get("paidMinor", 0),
        "pendingMinor": stored.get("pendingMinor", 0),
        "forecastMinor": stored.get("forecastMinor", 0),
        "forecastDeltaPct": stored.get("forecastDeltaPct", 0),
        "byMilestone": stored.get("byMilestone", []),
        "byRole": stored.get("byRole", []),
    }


def _to_project_detail(row: dict) -> dict:
    """Map stored project JSONB row → ProjectDetail shape the FE expects."""
    now = db.now_iso()
    return {
        # ── summary fields ────────────────────────────────────────────────────
        "id": row.get("id", ""),
        "name": row.get("name") or row.get("projectTitle") or row.get("clientOrganisation", "Unnamed project"),
        "sponsor": row.get("sponsor") or row.get("ownerEmail", ""),
        "pmo": row.get("pmo") or row.get("ownerEmail", ""),
        "startedAt": row.get("startedAt") or row.get("createdAt") or now,
        "dueAt": row.get("dueAt") or now,
        "progress": row.get("progress", 0),
        "health": row.get("health", "on_track"),
        "completedAt": row.get("completedAt"),
        "sowId": row.get("sowId"),
        "planId": row.get("planId"),
        # ── detail-only KPI fields ────────────────────────────────────────────
        "slaAtRiskCount": row.get("slaAtRiskCount", 0),
        "qualityAcceptanceRate": row.get("qualityAcceptanceRate", 1.0),
        "budgetBurnMinor": row.get("budgetBurnMinor", 0),
        "budgetTotalMinor": row.get("budgetTotalMinor", 0),
        # ── collections (default to empty so FE never crashes) ────────────────
        "milestones": row.get("milestones", []),
        "recentActivity": row.get("recentActivity", []),
        "aiSignals": row.get("aiSignals", []),
        "contributors": row.get("contributors", []),
        "reviewers": row.get("reviewers", []),
        "openExceptions": row.get("openExceptions", []),
        "resolvedExceptions": row.get("resolvedExceptions", []),
        "budget": _default_budget(row),
        "tasks": row.get("tasks", []),
        "audit": row.get("audit", []),
    }


def _to_project_summary(row: dict) -> dict:
    """Map stored project JSONB row → ProjectSummary shape."""
    now = db.now_iso()
    return {
        "id": row.get("id", ""),
        "name": row.get("name") or row.get("projectTitle") or row.get("clientOrganisation", "Unnamed project"),
        "sponsor": row.get("sponsor") or row.get("ownerEmail", ""),
        "pmo": row.get("pmo") or row.get("ownerEmail", ""),
        "startedAt": row.get("startedAt") or row.get("createdAt") or now,
        "dueAt": row.get("dueAt") or now,
        "progress": row.get("progress", 0),
        "health": row.get("health", "on_track"),
        "completedAt": row.get("completedAt"),
        "sowId": row.get("sowId"),
        "planId": row.get("planId"),
    }


# ── routes ─────────────────────────────────────────────────────────────────────

@portfolio_router.get("/projects")
def portfolio_projects(user: Annotated[dict, Depends(get_current_user)]):
    ensure_demo_data(user)
    projects = db.list_rows("project", user["id"])
    return {"projects": projects}


# GET /api/v1/projects — list all projects as ProjectSummary array
@projects_router.get("")
def list_projects(user: Annotated[dict, Depends(get_current_user)]):
    """Return all projects for the current enterprise user as ProjectSummary."""
    ensure_demo_data(user)
    rows = db.list_rows("project", user["id"])
    return {"projects": [_to_project_summary(r) for r in rows]}


# GET /api/v1/projects/{project_id} — single project as ProjectDetail
@projects_router.get("/{project_id}")
def get_project_detail(project_id: str, user: Annotated[dict, Depends(get_current_user)]):
    """Return a single project as ProjectDetail. 404 if not owned by caller."""
    ensure_demo_data(user)
    row = _load_project(project_id, user["id"])
    return _to_project_detail(row)


# POST /api/v1/projects/{project_id}/milestones/{milestone_id}/accept
@projects_router.post("/{project_id}/milestones/{milestone_id}/accept")
def accept_milestone(
    project_id: str,
    milestone_id: str,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Accept a milestone (locked → payable). Idempotent if already accepted."""
    row = _load_project(project_id, user["id"])
    milestones = row.get("milestones") or []
    now = db.now_iso()
    found = False
    for m in milestones:
        if m.get("id") == milestone_id:
            found = True
            current_status = m.get("paymentStatus", "locked")
            if current_status == "paid":
                raise HTTPException(status_code=409, detail="Milestone already paid")
            m["accepted"] = True
            m["acceptedAt"] = m.get("acceptedAt") or now
            m["paymentStatus"] = "payable"
            break
    if not found:
        raise HTTPException(status_code=404, detail="Milestone not found")

    row["milestones"] = milestones
    row.setdefault("audit", []).insert(0, {
        "id": db.new_id("aud_"),
        "ts": now,
        "actor": user.get("email", user.get("id", "unknown")),
        "action": "milestone.accepted",
        "resource": milestone_id,
    })
    row["updatedAt"] = now
    db.update_row("project", project_id, row, user["id"])

    write_audit(
        actor_id=user["id"],
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="project_milestone_accept",
        target="enterprise_projects",
        target_id=project_id,
        details=f"Milestone {milestone_id} accepted",
        service="enterprise",
    )

    return _to_project_detail(row)


# POST /api/v1/projects/{project_id}/milestones/{milestone_id}/pay
@projects_router.post("/{project_id}/milestones/{milestone_id}/pay")
def pay_milestone(
    project_id: str,
    milestone_id: str,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Pay an accepted milestone (payable → paid). Must be in 'payable' state."""
    row = _load_project(project_id, user["id"])
    milestones = row.get("milestones") or []
    now = db.now_iso()
    found = False
    for m in milestones:
        if m.get("id") == milestone_id:
            found = True
            current_status = m.get("paymentStatus", "locked")
            if current_status == "paid":
                raise HTTPException(status_code=409, detail="Milestone already paid")
            if current_status != "payable":
                raise HTTPException(status_code=409, detail="Milestone must be accepted before payment")
            m["paymentStatus"] = "paid"
            m["paidAt"] = now
            break
    if not found:
        raise HTTPException(status_code=404, detail="Milestone not found")

    # Recompute budget burn
    paid_total = sum(m.get("amountMinor", 0) for m in milestones if m.get("paymentStatus") == "paid")
    row["budgetBurnMinor"] = paid_total
    row["milestones"] = milestones
    row.setdefault("audit", []).insert(0, {
        "id": db.new_id("aud_"),
        "ts": now,
        "actor": user.get("email", user.get("id", "unknown")),
        "action": "milestone.paid",
        "resource": milestone_id,
    })
    row["updatedAt"] = now
    db.update_row("project", project_id, row, user["id"])

    write_audit(
        actor_id=user["id"],
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="project_milestone_pay",
        target="enterprise_projects",
        target_id=project_id,
        details=f"Milestone {milestone_id} paid",
        service="enterprise",
    )

    return _to_project_detail(row)


@projects_router.get("/{project_id}/team-composition")
def team_composition(project_id: str, user: Annotated[dict, Depends(get_current_user)]):
    ensure_demo_data(user)
    row = _load_project(project_id, user["id"])
    return row.get("teamComposition", {"members": [], "openRoles": []})


@projects_router.get("/{project_id}/skill-coverage")
def skill_coverage(project_id: str, user: Annotated[dict, Depends(get_current_user)]):
    ensure_demo_data(user)
    row = _load_project(project_id, user["id"])
    return row.get("skillCoverage", {"skills": [], "overall": 0})


@projects_router.post("/{project_id}/skill-review-request")
def skill_review_request(project_id: str, user: Annotated[dict, Depends(get_current_user)],
                         body: dict = Body(default={})):
    row = _load_project(project_id, user["id"])
    request = {
        "id": db.new_id("srr_"),
        "projectId": project_id,
        "requestedBy": user.get("email"),
        "requestedAt": db.now_iso(),
        "status": "pending",
        "payload": body,
    }
    row.setdefault("skillReviewRequests", []).append(request)
    row["updatedAt"] = db.now_iso()
    db.update_row("project", project_id, row, user["id"])
    return request
