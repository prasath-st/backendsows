"""
Per-user demo seeding. On a user's first access we create one demo SOW and one
demo decomposition plan so the dashboards are never empty. Idempotent: keyed by
a deterministic id derived from the owner.
"""

from __future__ import annotations

from enterprise_app import db


def _demo_sow_payload(owner: dict, sow_id: str) -> dict:
    return {
        "id": sow_id,
        "source": "ai",
        "projectTitle": "Demo Cloud Migration SOW",
        "clientOrganisation": "Acme Corp",
        "status": "draft",
        "ownerEmail": owner.get("email"),
        "createdAt": db.now_iso(),
        "updatedAt": db.now_iso(),
        "summary": "AI-generated statement of work for a phased cloud migration.",
        "sections": [
            {"key": "scope", "title": "Scope of Work", "content": "Migrate on-prem workloads to cloud in 3 phases.", "complete": True},
            {"key": "deliverables", "title": "Deliverables", "content": "Migration plan, runbooks, cutover.", "complete": False},
            {"key": "timeline", "title": "Timeline", "content": "12 weeks.", "complete": False},
        ],
        "clauses": [
            {"id": "c1", "title": "Confidentiality", "text": "Both parties keep information confidential."},
            {"id": "c2", "title": "Liability", "text": "Liability capped at fees paid."},
        ],
        "extractionItems": [
            {"id": "e1", "label": "Project budget", "value": "$250,000", "reviewState": "pending"},
            {"id": "e2", "label": "Start date", "value": "2026-07-01", "reviewState": "pending"},
        ],
        "gapItems": [
            {"id": "g1", "label": "Missing acceptance criteria", "resolved": False},
        ],
        "commercialDetails": {
            "pricing": {"model": "fixed", "amount": 250000, "currency": "USD"},
            "payment": {"terms": "Net 30", "milestones": []},
            "completeSections": [],
        },
        "approvalStages": [
            {"key": "legal", "title": "Legal Review", "status": "pending"},
            {"key": "finance", "title": "Finance Review", "status": "pending"},
            {"key": "exec", "title": "Executive Sign-off", "status": "pending"},
        ],
        "approvalAuthorities": [],
        "approvalMessages": [],
        "hallucinationLayers": [
            {"layer": "factual", "score": 0.92, "issues": []},
            {"layer": "consistency", "score": 0.88, "issues": []},
        ],
        "riskAssessment": {"overall": "low", "factors": []},
    }


def _demo_plan_payload(owner: dict, plan_id: str, sow_id: str) -> dict:
    return {
        "id": plan_id,
        "sowId": sow_id,
        "title": "Demo Cloud Migration Plan",
        "status": "draft",
        "ownerEmail": owner.get("email"),
        "createdAt": db.now_iso(),
        "updatedAt": db.now_iso(),
        "revision": 1,
        "locked": False,
        "confirmed": False,
        "summary": {"taskCount": 2, "milestoneCount": 1, "completion": 0},
        "checklistStatus": {"complete": False, "items": 3, "done": 0},
        "tasks": [
            {
                "id": "t1", "title": "Assess current workloads", "status": "todo",
                "owner": None, "flagged": False, "subtasks": [
                    {"id": "s1", "title": "Inventory servers", "status": "todo"}
                ],
                "detail": {"description": "Catalogue all workloads.", "estimateDays": 5},
            },
            {
                "id": "t2", "title": "Design target architecture", "status": "todo",
                "owner": None, "flagged": False, "subtasks": [],
                "detail": {"description": "Cloud landing zone design.", "estimateDays": 8},
            },
        ],
        "milestones": [
            {"id": "m1", "title": "Phase 1 complete", "date": "2026-08-15", "taskIds": ["t1"]},
        ],
        "criticalPath": ["t1", "t2"],
        "checklist": [
            {"id": "ck1", "label": "Scope confirmed", "done": False},
            {"id": "ck2", "label": "Resources assigned", "done": False},
            {"id": "ck3", "label": "Dates validated", "done": False},
        ],
        "review": {
            "status": "not_started",
            "checklist": [{"id": "rk1", "label": "Tasks reviewed", "done": False}],
            "summary": {},
        },
        "revisions": [],
    }


def _demo_project_payload(owner: dict, project_id: str, plan_id: str) -> dict:
    return {
        "id": project_id,
        "planId": plan_id,
        "name": "Demo Cloud Migration",
        "clientOrganisation": "Acme Corp",
        "status": "active",
        "progress": 0,
        "createdAt": db.now_iso(),
        "teamComposition": {
            "members": [
                {"id": "u1", "name": "Jane Doe", "role": "Lead Engineer", "allocation": 1.0},
                {"id": "u2", "name": "John Smith", "role": "Cloud Architect", "allocation": 0.5},
            ],
            "openRoles": [{"role": "QA Engineer", "count": 1}],
        },
        "skillCoverage": {
            "skills": [
                {"skill": "AWS", "coverage": 0.8, "required": True},
                {"skill": "Terraform", "coverage": 0.6, "required": True},
                {"skill": "Kubernetes", "coverage": 0.4, "required": False},
            ],
            "overall": 0.62,
        },
    }


def ensure_demo_data(owner: dict) -> None:
    """Create demo SOW + plan + project for this owner if none exist yet.

    DISABLED by default: auto-seeding re-creates a demo SOW every time an
    enterprise with zero SOWs loads a dashboard, which silently undoes a SOW
    purge. Set ENTERPRISE_DEMO_SEED=1 to re-enable for a fresh demo workspace."""
    import os
    if os.environ.get("ENTERPRISE_DEMO_SEED") != "1":
        return
    oid = owner.get("id")
    if not oid:
        return
    existing = db.list_rows("sow", oid)
    if existing:
        return
    sow_id = db.new_id("sow_")
    plan_id = db.new_id("plan_")
    project_id = db.new_id("proj_")
    db.create_row("sow", owner, _demo_sow_payload(owner, sow_id),
                  row_id=sow_id, extra_cols={"source": "ai"})
    db.create_row("plan", owner, _demo_plan_payload(owner, plan_id, sow_id), row_id=plan_id)
    db.create_row("project", owner, _demo_project_payload(owner, project_id, plan_id), row_id=project_id)
