"""Decomposition plans — /api/v1/enterprise/decomposition/**  (wrapped envelope).

A plan is a single JSONB document. Sub-resource GETs slice it; mutations merge
back into the document and persist. There is also a tiny internal router for the
revision-complete callback.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException

from shared.deps import get_current_user

from enterprise_app import db
from enterprise_app.responses import ok
from enterprise_app.seed import ensure_demo_data

router = APIRouter(prefix="/api/v1/enterprise/decomposition", tags=["decomposition"])
internal_router = APIRouter(prefix="/api/v1/internal/decomposition", tags=["decomposition-internal"])


def _load(plan_id: str, owner_id: str) -> dict:
    row = db.get_row("plan", plan_id, owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return row


def _save(plan_id: str, owner_id: str, row: dict) -> dict:
    row["updatedAt"] = db.now_iso()
    return db.update_row("plan", plan_id, row, owner_id)


def _slice(plan_id: str, owner_id: str, key: str, default):
    return ok(_load(plan_id, owner_id).get(key, default))


def _find_task(row: dict, task_id: str) -> dict | None:
    for t in row.get("tasks", []):
        if t.get("id") == task_id:
            return t
    return None


# ── plans collection ────────────────────────────────────────────────────────---

@router.get("/plans")
def list_plans(user: Annotated[dict, Depends(get_current_user)]):
    ensure_demo_data(user)
    return ok(db.list_rows("plan", user["id"]))


@router.post("/plans")
def create_plan(user: Annotated[dict, Depends(get_current_user)],
                body: dict = Body(default={})):
    plan_id = db.new_id("plan_")
    payload = {
        "id": plan_id,
        "title": (body or {}).get("title") or "Untitled Plan",
        "sowId": (body or {}).get("sowId"),
        "status": "draft",
        "revision": 1,
        "locked": False,
        "confirmed": False,
        "createdAt": db.now_iso(),
        "updatedAt": db.now_iso(),
        "tasks": [],
        "milestones": [],
        "criticalPath": [],
        "checklist": [],
        "review": {"status": "not_started", "checklist": [], "summary": {}},
        "revisions": [],
        "summary": {"taskCount": 0, "milestoneCount": 0, "completion": 0},
        "checklistStatus": {"complete": False, "items": 0, "done": 0},
        "data": dict(body or {}),
    }
    saved = db.create_row("plan", user, payload, row_id=plan_id)
    return ok(saved)


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    ensure_demo_data(user)
    return ok(_load(plan_id, user["id"]))


# ── revision ────────────────────────────────────────────────────────────────---

@router.get("/plans/{plan_id}/revision")
def get_revision(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    return ok({"revision": row.get("revision", 1), "revisions": row.get("revisions", [])})


@router.post("/plans/{plan_id}/revision")
@router.post("/plans/{plan_id}/revisions")  # plural alias — same handler
def create_revision(plan_id: str, user: Annotated[dict, Depends(get_current_user)],
                    body: dict = Body(default={})):
    row = _load(plan_id, user["id"])
    rev_no = row.get("revision", 1) + 1
    row["revision"] = rev_no
    rid = db.new_id("rev_")
    row.setdefault("revisions", []).append({
        "id": rid, "number": rev_no, "createdAt": db.now_iso(),
        "by": user.get("email"), "notes": (body or {}).get("notes"), "data": body,
    })
    return ok(_save(plan_id, user["id"], row))


@router.get("/plans/{plan_id}/summary")
def plan_summary(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    tasks = row.get("tasks", [])
    done = len([t for t in tasks if t.get("status") in ("done", "complete")])
    summary = {
        "taskCount": len(tasks),
        "milestoneCount": len(row.get("milestones", [])),
        "completion": round(done / len(tasks), 2) if tasks else 0,
        "status": row.get("status"),
    }
    return ok(summary)


@router.get("/plans/{plan_id}/checklist-status")
def checklist_status(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    items = row.get("checklist", [])
    done = len([c for c in items if c.get("done")])
    return ok({"items": len(items), "done": done, "complete": len(items) > 0 and done == len(items)})


@router.get("/plans/{plan_id}/status")
def plan_status(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    return ok({"status": row.get("status"), "locked": row.get("locked", False),
               "confirmed": row.get("confirmed", False)})


@router.post("/plans/{plan_id}/lock")
def lock_plan(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    row["locked"] = True
    return ok(_save(plan_id, user["id"], row))


@router.post("/plans/{plan_id}/confirm")
def confirm_plan(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    row["confirmed"] = True
    row["status"] = "confirmed"
    return ok(_save(plan_id, user["id"], row))


@router.post("/plans/{plan_id}/request-revision")
def request_revision(plan_id: str, user: Annotated[dict, Depends(get_current_user)],
                     body: dict = Body(default={})):
    row = _load(plan_id, user["id"])
    row["status"] = "revision_requested"
    row["revisionRequest"] = {"by": user.get("email"), "at": db.now_iso(),
                              "reason": (body or {}).get("reason"), "payload": body}
    return ok(_save(plan_id, user["id"], row))


@router.get("/plans/{plan_id}/revision-modal")
def revision_modal(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    return ok({"revision": row.get("revision", 1),
               "revisionRequest": row.get("revisionRequest"),
               "revisions": row.get("revisions", [])})


@router.get("/plans/{plan_id}/revised")
def revised(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    return ok({"revision": row.get("revision", 1), "data": row})


@router.get("/plans/{plan_id}/revisions/{rid}")
def get_revision_by_id(plan_id: str, rid: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    for r in row.get("revisions", []):
        if r.get("id") == rid:
            return ok(r)
    raise HTTPException(status_code=404, detail="Revision not found")


# ── review ──────────────────────────────────────────────────────────────────---

@router.get("/plans/{plan_id}/review")
def get_review(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    return _slice(plan_id, user["id"], "review", {"status": "not_started", "checklist": [], "summary": {}})


@router.get("/plans/{plan_id}/review/checklist")
def get_review_checklist(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    return ok(row.get("review", {}).get("checklist", []))


@router.patch("/plans/{plan_id}/review/checklist")
def patch_review_checklist(plan_id: str, user: Annotated[dict, Depends(get_current_user)],
                           body: dict = Body(default={})):
    row = _load(plan_id, user["id"])
    review = row.setdefault("review", {"status": "in_progress", "checklist": [], "summary": {}})
    item_id = (body or {}).get("id")
    checklist = review.setdefault("checklist", [])
    if item_id:
        for c in checklist:
            if c.get("id") == item_id:
                c.update(body)
                break
        else:
            checklist.append(body)
    elif isinstance((body or {}).get("checklist"), list):
        review["checklist"] = body["checklist"]
    return ok(_save(plan_id, user["id"], row).get("review"))


@router.get("/plans/{plan_id}/review/summary")
def review_summary(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    review = row.get("review", {})
    checklist = review.get("checklist", [])
    done = len([c for c in checklist if c.get("done")])
    return ok({"status": review.get("status"), "items": len(checklist), "done": done,
               "summary": review.get("summary", {})})


# ── tasks ───────────────────────────────────────────────────────────────────---

@router.get("/plans/{plan_id}/tasks")
def list_tasks(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    return _slice(plan_id, user["id"], "tasks", [])


@router.post("/plans/{plan_id}/tasks/query")
def query_tasks(plan_id: str, user: Annotated[dict, Depends(get_current_user)],
                body: dict = Body(default={})):
    row = _load(plan_id, user["id"])
    tasks = row.get("tasks", [])
    status = (body or {}).get("status")
    flagged = (body or {}).get("flagged")
    if status is not None:
        tasks = [t for t in tasks if t.get("status") == status]
    if flagged is not None:
        tasks = [t for t in tasks if bool(t.get("flagged")) == bool(flagged)]
    return ok(tasks)


@router.get("/plans/{plan_id}/tasks/{task_id}")
def get_task(plan_id: str, task_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    task = _find_task(row, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return ok(task)


@router.post("/plans/{plan_id}/tasks")
def create_task(plan_id: str, user: Annotated[dict, Depends(get_current_user)],
                body: dict = Body(default={})):
    row = _load(plan_id, user["id"])
    tid = (body or {}).get("id") or db.new_id("task_")
    task = {
        "id": tid,
        "title": (body or {}).get("title") or "New Task",
        "status": (body or {}).get("status") or "todo",
        "owner": (body or {}).get("owner"),
        "flagged": False,
        "subtasks": (body or {}).get("subtasks", []),
        "detail": (body or {}).get("detail", {}),
    }
    task.update({k: v for k, v in (body or {}).items() if k not in task})
    row.setdefault("tasks", []).append(task)
    _save(plan_id, user["id"], row)
    return ok(task)


@router.get("/contributors")
def list_assignable_contributors(user: Annotated[dict, Depends(get_current_user)]):
    """Approved contributors the enterprise can assign tasks to (for the
    assign-task dropdown)."""
    from shared.db import get_pg_connection
    from psycopg2.extras import RealDictCursor
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, email, first_name, last_name, role FROM login_accounts "
            "WHERE role = 'contributor' AND COALESCE(approval_status,'approved') = 'approved' "
            "ORDER BY created_at DESC LIMIT 100")
        out = [{"id": str(r["id"]), "email": r["email"],
                "name": (f"{r.get('first_name','') or ''} {r.get('last_name','') or ''}".strip() or r["email"])}
               for r in cur.fetchall()]
    return ok(out)


@router.post("/plans/{plan_id}/tasks/{task_id}/assign")
def assign_task(plan_id: str, task_id: str, user: Annotated[dict, Depends(get_current_user)],
                body: dict = Body(default={})):
    """Assign a plan task to a contributor. Creates a contributor_tasks row so it
    appears in that contributor's task list. If no contributor is given, the task
    is left 'available' (open pool) — but here we require an explicit assignee."""
    row = _load(plan_id, user["id"])
    task = _find_task(row, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    account_id = (body or {}).get("contributorId") or (body or {}).get("accountId")
    account_email = (body or {}).get("contributorEmail")
    sow_id = row.get("sowId")

    from shared.db import get_pg_connection
    from psycopg2.extras import Json
    conn = get_pg_connection()
    with conn.cursor() as cur:
        # Mark the plan task assigned.
        task["status"] = "assigned"
        task["assigneeId"] = str(account_id) if account_id else None
        task["assigneeEmail"] = account_email
        # Create the contributor-facing task (status assigned → shows in their list).
        if account_id:
            cur.execute(
                "INSERT INTO contributor_tasks (account_id, title, status, priority, category, data) "
                "VALUES (%s,%s,'assigned',%s,%s,%s) RETURNING id",
                (int(account_id), task.get("title") or "Task", "normal", "delivery",
                 Json({"planId": plan_id, "planTaskId": task_id, "sowId": sow_id,
                       "description": (task.get("detail") or {}).get("description") or "",
                       "skills_required": (body or {}).get("skills") or []})))
            ctid = cur.fetchone()[0]
            task["contributorTaskId"] = ctid
        conn.commit()
    _save(plan_id, user["id"], row)
    return ok(task)


@router.patch("/plans/{plan_id}/tasks/{task_id}")
def patch_task(plan_id: str, task_id: str, user: Annotated[dict, Depends(get_current_user)],
               body: dict = Body(default={})):
    row = _load(plan_id, user["id"])
    task = _find_task(row, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    task.update(body or {})
    _save(plan_id, user["id"], row)
    return ok(task)


@router.delete("/plans/{plan_id}/tasks/{task_id}")
def delete_task(plan_id: str, task_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    before = len(row.get("tasks", []))
    row["tasks"] = [t for t in row.get("tasks", []) if t.get("id") != task_id]
    if len(row["tasks"]) == before:
        raise HTTPException(status_code=404, detail="Task not found")
    _save(plan_id, user["id"], row)
    return ok({"deleted": True, "id": task_id})


@router.get("/plans/{plan_id}/tasks/{task_id}/detail")
def task_detail(plan_id: str, task_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    task = _find_task(row, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return ok({**task, "detail": task.get("detail", {})})


@router.post("/plans/{plan_id}/tasks/{task_id}/flag")
def flag_task(plan_id: str, task_id: str, user: Annotated[dict, Depends(get_current_user)],
              body: dict = Body(default={})):
    row = _load(plan_id, user["id"])
    task = _find_task(row, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    task["flagged"] = (body or {}).get("flagged", True)
    task["flagReason"] = (body or {}).get("reason")
    _save(plan_id, user["id"], row)
    return ok(task)


@router.post("/plans/{plan_id}/tasks/{task_id}/subtasks")
def add_subtask(plan_id: str, task_id: str, user: Annotated[dict, Depends(get_current_user)],
                body: dict = Body(default={})):
    row = _load(plan_id, user["id"])
    task = _find_task(row, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    sid = (body or {}).get("id") or db.new_id("sub_")
    sub = {"id": sid, "title": (body or {}).get("title") or "Subtask",
           "status": (body or {}).get("status") or "todo"}
    sub.update({k: v for k, v in (body or {}).items() if k not in sub})
    task.setdefault("subtasks", []).append(sub)
    _save(plan_id, user["id"], row)
    return ok(sub)


@router.patch("/plans/{plan_id}/tasks/{task_id}/subtasks/{subtask_id}")
def patch_subtask(plan_id: str, task_id: str, subtask_id: str,
                  user: Annotated[dict, Depends(get_current_user)],
                  body: dict = Body(default={})):
    row = _load(plan_id, user["id"])
    task = _find_task(row, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    for s in task.get("subtasks", []):
        if s.get("id") == subtask_id:
            s.update(body or {})
            _save(plan_id, user["id"], row)
            return ok(s)
    raise HTTPException(status_code=404, detail="Subtask not found")


@router.delete("/plans/{plan_id}/tasks/{task_id}/subtasks/{subtask_id}")
def delete_subtask(plan_id: str, task_id: str, subtask_id: str,
                   user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    task = _find_task(row, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    before = len(task.get("subtasks", []))
    task["subtasks"] = [s for s in task.get("subtasks", []) if s.get("id") != subtask_id]
    if len(task["subtasks"]) == before:
        raise HTTPException(status_code=404, detail="Subtask not found")
    _save(plan_id, user["id"], row)
    return ok({"deleted": True, "id": subtask_id})


# ── milestones / critical path ──────────────────────────────────────────────---

@router.get("/plans/{plan_id}/milestones")
def milestones(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    return _slice(plan_id, user["id"], "milestones", [])


@router.get("/plans/{plan_id}/critical-path")
def critical_path(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    return _slice(plan_id, user["id"], "criticalPath", [])


# ── checklist ───────────────────────────────────────────────────────────────---

@router.get("/plans/{plan_id}/checklist")
def get_checklist(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    return _slice(plan_id, user["id"], "checklist", [])


@router.post("/plans/{plan_id}/checklist")
def add_checklist_item(plan_id: str, user: Annotated[dict, Depends(get_current_user)],
                       body: dict = Body(default={})):
    row = _load(plan_id, user["id"])
    cid = (body or {}).get("id") or db.new_id("ck_")
    item = {"id": cid, "label": (body or {}).get("label") or "Item",
            "done": (body or {}).get("done", False)}
    row.setdefault("checklist", []).append(item)
    _save(plan_id, user["id"], row)
    return ok(item)


@router.post("/plans/{plan_id}/checklist/validate")
def validate_checklist(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    items = row.get("checklist", [])
    incomplete = [c for c in items if not c.get("done")]
    return ok({"valid": len(incomplete) == 0, "incomplete": incomplete})


@router.post("/plans/{plan_id}/checklist/date-validation")
def checklist_date_validation(plan_id: str, user: Annotated[dict, Depends(get_current_user)],
                              body: dict = Body(default={})):
    row = _load(plan_id, user["id"])
    issues = []
    for m in row.get("milestones", []):
        if not m.get("date"):
            issues.append({"milestoneId": m.get("id"), "issue": "missing date"})
    return ok({"valid": len(issues) == 0, "issues": issues})


@router.get("/plans/{plan_id}/summary-panel")
def summary_panel(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    tasks = row.get("tasks", [])
    done = len([t for t in tasks if t.get("status") in ("done", "complete")])
    return ok({
        "title": row.get("title"),
        "status": row.get("status"),
        "taskCount": len(tasks),
        "completedTasks": done,
        "milestoneCount": len(row.get("milestones", [])),
        "locked": row.get("locked", False),
        "confirmed": row.get("confirmed", False),
        "revision": row.get("revision", 1),
    })


@router.get("/plans/{plan_id}/configure-legacy")
def configure_legacy(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    return ok({"planId": plan_id, "legacy": True, "config": row.get("data", {})})


# ── plan actions (query-param style) ────────────────────────────────────────---

@router.post("/plans/actions/kickoff")
def kickoff(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    row["status"] = "kickoff_requested"
    row["kickoff"] = {"requestedBy": user.get("email"), "at": db.now_iso(), "approved": False}
    return ok(_save(plan_id, user["id"], row))


@router.post("/plans/actions/approve-kickoff")
def approve_kickoff(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    row["status"] = "kickoff_approved"
    kickoff = row.setdefault("kickoff", {})
    kickoff["approved"] = True
    kickoff["approvedBy"] = user.get("email")
    kickoff["approvedAt"] = db.now_iso()
    return ok(_save(plan_id, user["id"], row))


@router.delete("/plans/actions/{plan_id}/withdraw")
def withdraw(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    row["status"] = "withdrawn"
    return ok(_save(plan_id, user["id"], row))


@router.post("/plans/{plan_id}/submit-for-review")
def submit_for_review(plan_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(plan_id, user["id"])
    row["status"] = "in_review"
    review = row.setdefault("review", {"checklist": [], "summary": {}})
    review["status"] = "in_progress"
    review["submittedAt"] = db.now_iso()
    return ok(_save(plan_id, user["id"], row))


# ── internal revision-complete callback ─────────────────────────────────────---

@internal_router.post("/plans/{plan_id}/revision/complete")
def revision_complete(plan_id: str, user: Annotated[dict, Depends(get_current_user)],
                      body: dict = Body(default={})):
    row = db.get_row("plan", plan_id, user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    row["status"] = "revised"
    revs = row.get("revisions", [])
    if revs:
        revs[-1]["completedAt"] = db.now_iso()
        revs[-1]["result"] = body
    row["updatedAt"] = db.now_iso()
    saved = db.update_row("plan", plan_id, row, user["id"])
    return ok(saved)
