"""
Decomposition plans — normalised Postgres implementation.

Endpoints:
  GET    /api/v1/enterprise/decomposition/plans
  POST   /api/v1/enterprise/decomposition/plans
  GET    /api/v1/enterprise/decomposition/plans/{plan_id}
  PATCH  /api/v1/enterprise/decomposition/plans/{plan_id}
  POST   /api/v1/enterprise/decomposition/plans/{plan_id}/activate
  POST   /api/v1/enterprise/decomposition/plans/{plan_id}/approve
  POST   /api/v1/enterprise/decomposition/plans/{plan_id}/archive
  POST   /api/v1/enterprise/decomposition/plans/{plan_id}/copy

These match the FE proxy path /api/decomposition/plans → /api/v1/enterprise/decomposition/plans.

Response shapes match the FE lib/decomposition/types.ts:
  - Single plan: { plan: PlanDetail }
  - List:        { items: PlanSummary[], nextCursor: string | null }
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from psycopg2.extras import Json, RealDictCursor

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/enterprise/decomposition",
    tags=["decomposition-plans"],
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "") -> str:
    base = uuid.uuid4().hex
    return f"{prefix}{base}" if prefix else base


def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def _to_iso(val) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


# ── row → shape mappers ───────────────────────────────────────────────────────


def _plan_summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "sowId": row["sow_id"],
        "version": row["version"],
        "status": row["status"],
        "summary": row["summary"],
        "defaultWorkforceSourcing": row.get("default_workforce_sourcing"),
        "defaultReviewPath": row.get("default_review_path"),
        "twoStageReviewEnabled": bool(row.get("two_stage_review_enabled", False)),
        "approvedAt": _to_iso(row.get("approved_at")),
        "approvedBy": row.get("approved_by"),
        "activatedAt": _to_iso(row.get("activated_at")),
        "archivedAt": _to_iso(row.get("archived_at")),
        "createdBy": row["created_by"],
        "createdAt": _to_iso(row["created_at"]),
        "updatedAt": _to_iso(row["updated_at"]),
    }


def _milestone_detail(row: dict) -> dict:
    return {
        "id": row["id"],
        "order": row["order"],
        "name": row["name"],
        "description": row.get("description"),
        "startDate": _to_iso(row.get("start_date")),
        "endDate": _to_iso(row.get("end_date")),
        "status": row["status"],
        "createdAt": _to_iso(row["created_at"]),
        "updatedAt": _to_iso(row["updated_at"]),
    }


def _task_detail(row: dict) -> dict:
    skills = row.get("required_skills") or []
    if isinstance(skills, str):
        import json as _json
        try:
            skills = _json.loads(skills)
        except Exception:
            skills = []
    return {
        "id": row["id"],
        "milestoneId": row.get("milestone_id"),
        "externalKey": row.get("external_key"),
        "title": row["title"],
        "description": row.get("description"),
        "requiredSkills": skills,
        "estimatedHours": float(row["estimated_hours"]) if row.get("estimated_hours") is not None else None,
        "acceptanceCriteria": row.get("acceptance_criteria"),
        "complexity": row.get("complexity"),
        "order": row["order"],
        "status": row["status"],
        "aiConfidence": row.get("ai_confidence"),
        "pmoEdited": bool(row.get("pmo_edited", False)),
        "workforceSourcing": row.get("workforce_sourcing"),
        "reviewPath": row.get("review_path"),
        "createdAt": _to_iso(row["created_at"]),
        "updatedAt": _to_iso(row["updated_at"]),
    }


def _dep_detail(row: dict) -> dict:
    return {
        "id": row["id"],
        "fromTaskId": row["from_task_id"],
        "toTaskId": row["to_task_id"],
        "type": row["type"],
        "createdAt": _to_iso(row["created_at"]),
    }


# ── DB reads ─────────────────────────────────────────────────────────────────


def _get_plan_row(plan_id: str) -> dict | None:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM decomp_plans WHERE id = %s AND deleted_at IS NULL",
            [plan_id],
        )
        row = cur.fetchone()
    return dict(row) if row else None


def _get_plan_detail(plan_id: str) -> dict | None:
    plan = _get_plan_row(plan_id)
    if not plan:
        return None
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM decomp_milestones WHERE plan_id = %s ORDER BY \"order\" ASC",
            [plan_id],
        )
        milestones = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT * FROM decomp_tasks WHERE plan_id = %s ORDER BY \"order\" ASC",
            [plan_id],
        )
        tasks = [dict(r) for r in cur.fetchall()]
        task_ids = [t["id"] for t in tasks]
        deps: list[dict] = []
        if task_ids:
            cur.execute(
                "SELECT * FROM decomp_dependencies WHERE from_task_id = ANY(%s)",
                [task_ids],
            )
            deps = [dict(r) for r in cur.fetchall()]
    return {
        **_plan_summary(plan),
        "milestones": [_milestone_detail(m) for m in milestones],
        "tasks": [_task_detail(t) for t in tasks],
        "dependencies": [_dep_detail(d) for d in deps],
    }


# ── structure validation ──────────────────────────────────────────────────────


def _validate_structure(structure: dict) -> None:
    """Replicates FE service.ts validateStructure."""
    milestones = structure.get("milestones", [])
    tasks = structure.get("tasks", [])
    deps = structure.get("dependencies", [])

    ms_keys: dict[str, Any] = {}
    ms_orders: set[int] = set()
    for m in milestones:
        name = (m.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="milestone name required")
        order = m.get("order")
        if order in ms_orders:
            raise HTTPException(status_code=400, detail=f"duplicate milestone order: {order}")
        ms_orders.add(order)
        key = m.get("key")
        if key:
            if key in ms_keys:
                raise HTTPException(status_code=400, detail=f"duplicate milestone key: {key}")
            ms_keys[key] = m

    task_keys: dict[str, Any] = {}
    for t in tasks:
        title = (t.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="task title required")
        key = t.get("key")
        if key:
            if key in task_keys:
                raise HTTPException(status_code=400, detail=f"duplicate task key: {key}")
            task_keys[key] = t
        mk = t.get("milestoneKey")
        if mk and mk not in ms_keys:
            raise HTTPException(status_code=400, detail=f"task references unknown milestoneKey: {mk}")

    for d in deps:
        fk = d.get("fromTaskKey", "")
        tk = d.get("toTaskKey", "")
        if fk not in task_keys:
            raise HTTPException(status_code=400, detail=f"dependency fromTaskKey unknown: {fk}")
        if tk not in task_keys:
            raise HTTPException(status_code=400, detail=f"dependency toTaskKey unknown: {tk}")
        if fk == tk:
            raise HTTPException(status_code=400, detail=f"dependency cannot point at itself: {fk}")

    # DAG cycle detection
    adjacency: dict[str, list[str]] = {k: [] for k in task_keys}
    for d in deps:
        adjacency[d["fromTaskKey"]].append(d["toTaskKey"])

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in task_keys}

    def dfs(node: str) -> None:
        color[node] = GRAY
        for nxt in adjacency.get(node, []):
            c = color.get(nxt, WHITE)
            if c == GRAY:
                raise HTTPException(status_code=400, detail=f"dependency cycle detected: {node} -> {nxt}")
            if c == WHITE:
                dfs(nxt)
        color[node] = BLACK

    for node in task_keys:
        if color.get(node, WHITE) == WHITE:
            dfs(node)


def _write_structure(plan_id: str, tenant_id: str, structure: dict, conn) -> None:
    """Insert milestones, tasks, dependencies for a plan (assumes clean slate)."""
    milestones = structure.get("milestones", [])
    tasks = structure.get("tasks", [])
    deps = structure.get("dependencies", [])

    ms_key_to_id: dict[str, str] = {}
    with conn.cursor() as cur:
        for m in milestones:
            mid = _new_id("ms_")
            cur.execute(
                """INSERT INTO decomp_milestones
                    (id, plan_id, tenant_id, "order", name, description,
                     start_date, end_date, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [
                    mid, plan_id, tenant_id,
                    m.get("order", 0), m["name"], m.get("description"),
                    m.get("startDate"), m.get("endDate"),
                    m.get("status") or "pending",
                ],
            )
            if m.get("key"):
                ms_key_to_id[m["key"]] = mid

        task_key_to_id: dict[str, str] = {}
        for t in tasks:
            tid = _new_id("tsk_")
            milestone_id = ms_key_to_id.get(t["milestoneKey"]) if t.get("milestoneKey") else None
            cur.execute(
                """INSERT INTO decomp_tasks
                    (id, plan_id, milestone_id, tenant_id, external_key,
                     title, description, required_skills, estimated_hours,
                     acceptance_criteria, complexity, "order", status,
                     ai_confidence, pmo_edited, workforce_sourcing, review_path)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [
                    tid, plan_id, milestone_id, tenant_id,
                    t.get("externalKey"),
                    t["title"], t.get("description"),
                    Json(t.get("requiredSkills") or []),
                    t.get("estimatedHours"),
                    t.get("acceptanceCriteria"),
                    t.get("complexity"),
                    t.get("order") or 0,
                    "draft",
                    t.get("aiConfidence"),
                    bool(t.get("pmoEdited", False)),
                    t.get("workforceSourcing"),
                    t.get("reviewPath"),
                ],
            )
            if t.get("key"):
                task_key_to_id[t["key"]] = tid

        for d in deps:
            did = _new_id("dep_")
            from_id = task_key_to_id.get(d["fromTaskKey"])
            to_id = task_key_to_id.get(d["toTaskKey"])
            if from_id and to_id:
                cur.execute(
                    """INSERT INTO decomp_dependencies
                        (id, from_task_id, to_task_id, tenant_id, type)
                       VALUES (%s, %s, %s, %s, %s)""",
                    [did, from_id, to_id, tenant_id, d.get("type") or "finish_to_start"],
                )


# ── GET /plans ────────────────────────────────────────────────────────────────


@router.get("/plans")
def list_plans(
    user: Annotated[dict, Depends(get_current_user)],
    sow_id: str | None = Query(default=None, alias="sowId"),
    status: list[str] | None = Query(default=None),
    include_archived: bool = Query(default=False, alias="includeArchived"),
    limit: int = Query(default=50, le=100),
    cursor: str | None = Query(default=None),
):
    valid_statuses = {"draft", "approved", "active", "archived"}
    if status:
        bad = [s for s in status if s not in valid_statuses]
        if bad:
            raise HTTPException(status_code=400, detail=f"Invalid status: {bad[0]}")

    tenant_id = user.get("tenant_id") or user.get("id")
    conn = _conn()
    params: list[Any] = [tenant_id]
    clauses = ["tenant_id = %s", "deleted_at IS NULL"]

    if sow_id:
        clauses.append("sow_id = %s")
        params.append(sow_id)

    if status:
        placeholders = ", ".join(["%s"] * len(status))
        clauses.append(f"status IN ({placeholders})")
        params.extend(status)
    elif not include_archived:
        clauses.append("status != 'archived'")

    if cursor:
        clauses.append("id > %s")
        params.append(cursor)

    where = " AND ".join(clauses)
    params.append(limit + 1)
    sql = f"SELECT * FROM decomp_plans WHERE {where} ORDER BY sow_id ASC, version DESC LIMIT %s"

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]

    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = rows[limit - 1]["id"] if has_more else None

    return {"items": [_plan_summary(r) for r in items], "nextCursor": next_cursor}


# ── POST /plans ───────────────────────────────────────────────────────────────


@router.post("/plans", status_code=201)
def create_plan(
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
):
    body = body or {}
    sow_id = body.get("sowId") or body.get("sow_id") or ""
    if not sow_id:
        raise HTTPException(status_code=400, detail="sowId is required")

    tenant_id = user.get("tenant_id") or user.get("id")
    created_by = user.get("id") or user.get("email") or "unknown"

    structure = body.get("structure")
    if structure:
        _validate_structure(structure)

    conn = _conn()

    # pick next version per sow_id
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT MAX(version) AS v FROM decomp_plans WHERE sow_id = %s AND deleted_at IS NULL",
            [sow_id],
        )
        row = cur.fetchone()
    next_version = (row["v"] or 0) + 1 if row and row["v"] is not None else 1

    plan_id = _new_id("dp_")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO decomp_plans
                (id, tenant_id, sow_id, version, status, summary,
                 source_agent_invocation_id, created_by)
               VALUES (%s, %s, %s, %s, 'draft', %s, %s, %s)""",
            [
                plan_id, tenant_id, sow_id, next_version,
                body.get("summary"),
                body.get("sourceAgentInvocationId"),
                created_by,
            ],
        )
    if structure:
        _write_structure(plan_id, tenant_id, structure, conn)
    conn.commit()

    plan = _get_plan_detail(plan_id)
    _audit(user, "decomposition.plan.create", plan_id, extra={
        "sowId": sow_id, "version": next_version,
        "milestoneCount": len(plan.get("milestones", [])) if plan else 0,
        "taskCount": len(plan.get("tasks", [])) if plan else 0,
    })
    return {"plan": plan}


# ── GET /plans/{plan_id} ──────────────────────────────────────────────────────


@router.get("/plans/{plan_id}")
def get_plan(
    plan_id: str,
    user: Annotated[dict, Depends(get_current_user)],
):
    plan = _get_plan_detail(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return {"plan": plan}


# ── PATCH /plans/{plan_id} ────────────────────────────────────────────────────


@router.patch("/plans/{plan_id}")
def update_plan(
    plan_id: str,
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
):
    body = body or {}
    if "summary" not in body and "structure" not in body:
        raise HTTPException(
            status_code=400,
            detail="Request body must include summary and/or structure",
        )

    row = _get_plan_row(plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="Plan not found")
    if row["status"] != "draft":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot edit a plan in '{row['status']}' state",
        )

    structure = body.get("structure")
    if structure:
        _validate_structure(structure)

    conn = _conn()
    if structure:
        # atomic replace
        with conn.cursor() as cur:
            # collect task ids first for dep cleanup
            cur.execute("SELECT id FROM decomp_tasks WHERE plan_id = %s", [plan_id])
            task_ids = [r[0] for r in cur.fetchall()]
            if task_ids:
                cur.execute(
                    "DELETE FROM decomp_dependencies WHERE from_task_id = ANY(%s)",
                    [task_ids],
                )
            cur.execute("DELETE FROM decomp_tasks WHERE plan_id = %s", [plan_id])
            cur.execute("DELETE FROM decomp_milestones WHERE plan_id = %s", [plan_id])
        tenant_id = row["tenant_id"]
        _write_structure(plan_id, tenant_id, structure, conn)

    if "summary" in body:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE decomp_plans SET summary = %s, updated_at = now() WHERE id = %s",
                [body["summary"], plan_id],
            )
    else:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE decomp_plans SET updated_at = now() WHERE id = %s",
                [plan_id],
            )
    conn.commit()

    plan = _get_plan_detail(plan_id)
    _audit(user, "decomposition.plan.update", plan_id, extra={
        "summaryChanged": "summary" in body,
        "structureReplaced": structure is not None,
    })
    return {"plan": plan}


# ── GET/POST /plans/{plan_id}/tasks ───────────────────────────────────────────


@router.get("/plans/{plan_id}/tasks")
def list_plan_tasks(
    plan_id: str,
    user: Annotated[dict, Depends(get_current_user)],
):
    if not _get_plan_row(plan_id):
        raise HTTPException(status_code=404, detail="Plan not found")
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            'SELECT * FROM decomp_tasks WHERE plan_id = %s ORDER BY "order", created_at',
            [plan_id],
        )
        rows = cur.fetchall()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@router.post("/plans/{plan_id}/tasks", status_code=201)
def create_plan_task(
    plan_id: str,
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
):
    row = _get_plan_row(plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="Plan not found")
    if row["status"] != "draft":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot add tasks to a plan in '{row['status']}' state",
        )
    body = body or {}
    title = body.get("title")
    if not title:
        raise HTTPException(status_code=422, detail="title is required")

    tid = _new_id("tsk_")
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO decomp_tasks
                (id, plan_id, milestone_id, tenant_id, external_key,
                 title, description, required_skills, estimated_hours,
                 acceptance_criteria, complexity, "order", status,
                 ai_confidence, pmo_edited, workforce_sourcing, review_path)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                tid, plan_id, body.get("milestoneId"), row["tenant_id"],
                body.get("externalKey"),
                title, body.get("description"),
                Json(body.get("requiredSkills") or []),
                body.get("estimatedHours"),
                body.get("acceptanceCriteria"),
                body.get("complexity"),
                body.get("order") or 0,
                "draft",
                body.get("aiConfidence"),
                bool(body.get("pmoEdited", False)),
                body.get("workforceSourcing"),
                body.get("reviewPath"),
            ],
        )
    conn.commit()
    _audit(user, "decomposition.task.create", plan_id, extra={"taskId": tid})
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM decomp_tasks WHERE id = %s", [tid])
        task = cur.fetchone()
    return {"task": dict(task) if task else {"id": tid}}


# ── POST /plans/{plan_id}/approve ─────────────────────────────────────────────


@router.post("/plans/{plan_id}/approve")
def approve_plan(
    plan_id: str,
    user: Annotated[dict, Depends(get_current_user)],
):
    row = _get_plan_row(plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="Plan not found")
    if row["status"] != "draft":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve a plan in '{row['status']}' state",
        )

    # ensure at least one task
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM decomp_tasks WHERE plan_id = %s", [plan_id])
        count = cur.fetchone()[0]
    if count == 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot approve an empty plan — add at least one task",
        )

    approver_id = user.get("id") or user.get("email") or "unknown"
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE decomp_plans
               SET status = 'approved', approved_at = now(), approved_by = %s, updated_at = now()
               WHERE id = %s""",
            [approver_id, plan_id],
        )
    conn.commit()

    plan = _get_plan_detail(plan_id)
    _audit(user, "decomposition.plan.approve", plan_id, extra={
        "sowId": row["sow_id"],
        "taskCount": len(plan.get("tasks", [])) if plan else 0,
    })
    return {"plan": plan}


# ── POST /plans/{plan_id}/activate ────────────────────────────────────────────


@router.post("/plans/{plan_id}/activate")
def activate_plan(
    plan_id: str,
    user: Annotated[dict, Depends(get_current_user)],
):
    row = _get_plan_row(plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="Plan not found")
    if row["status"] != "approved":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot activate a plan in '{row['status']}' state — approve it first",
        )

    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE decomp_plans
               SET status = 'active', activated_at = now(), updated_at = now()
               WHERE id = %s""",
            [plan_id],
        )
        # flip draft tasks to ready
        cur.execute(
            """UPDATE decomp_tasks
               SET status = 'ready', updated_at = now()
               WHERE plan_id = %s AND status = 'draft'""",
            [plan_id],
        )
    conn.commit()

    plan = _get_plan_detail(plan_id)
    readied = len([t for t in (plan.get("tasks") or []) if t["status"] == "ready"]) if plan else 0
    _audit(user, "decomposition.plan.activate", plan_id, extra={
        "sowId": row["sow_id"],
        "tasksReadied": readied,
    })
    return {"plan": plan}


# ── POST /plans/{plan_id}/archive ─────────────────────────────────────────────


@router.post("/plans/{plan_id}/archive")
def archive_plan(
    plan_id: str,
    user: Annotated[dict, Depends(get_current_user)],
):
    row = _get_plan_row(plan_id)
    if not row:
        raise HTTPException(status_code=404, detail="Plan not found")
    if row["status"] == "archived":
        raise HTTPException(status_code=400, detail="Plan is already archived")

    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE decomp_plans
               SET status = 'archived', archived_at = now(), updated_at = now()
               WHERE id = %s""",
            [plan_id],
        )
    conn.commit()

    plan = _get_plan_detail(plan_id)
    _audit(user, "decomposition.plan.archive", plan_id, extra={"sowId": row["sow_id"]})
    return {"plan": plan}


# ── POST /plans/{plan_id}/copy ────────────────────────────────────────────────


@router.post("/plans/{plan_id}/copy", status_code=201)
def copy_plan(
    plan_id: str,
    user: Annotated[dict, Depends(get_current_user)],
):
    source = _get_plan_detail(plan_id)
    if not source:
        raise HTTPException(status_code=404, detail="Plan not found")

    tenant_id = user.get("tenant_id") or user.get("id")
    created_by = user.get("id") or user.get("email") or "unknown"
    sow_id = source["sowId"]

    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT MAX(version) AS v FROM decomp_plans WHERE sow_id = %s AND deleted_at IS NULL",
            [sow_id],
        )
        row = cur.fetchone()
    next_version = (row["v"] or 0) + 1 if row and row["v"] is not None else 1

    new_plan_id = _new_id("dp_")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO decomp_plans
                (id, tenant_id, sow_id, version, status, summary, created_by)
               VALUES (%s, %s, %s, %s, 'draft', %s, %s)""",
            [new_plan_id, tenant_id, sow_id, next_version, source.get("summary"), created_by],
        )

    # rebuild structure from source detail
    ms_id_to_key: dict[str, str] = {}
    milestones_in = []
    for m in source.get("milestones", []):
        key = f"m-{m['id']}"
        ms_id_to_key[m["id"]] = key
        milestones_in.append({
            "key": key, "order": m["order"], "name": m["name"],
            "description": m.get("description"), "startDate": m.get("startDate"),
            "endDate": m.get("endDate"), "status": m.get("status", "pending"),
        })

    task_id_to_key: dict[str, str] = {}
    tasks_in = []
    for t in source.get("tasks", []):
        key = f"t-{t['id']}"
        task_id_to_key[t["id"]] = key
        tasks_in.append({
            "key": key,
            "milestoneKey": ms_id_to_key.get(t["milestoneId"]) if t.get("milestoneId") else None,
            "externalKey": t.get("externalKey"),
            "title": t["title"],
            "description": t.get("description"),
            "requiredSkills": t.get("requiredSkills", []),
            "estimatedHours": t.get("estimatedHours"),
            "acceptanceCriteria": t.get("acceptanceCriteria"),
            "complexity": t.get("complexity"),
            "order": t.get("order", 0),
            "aiConfidence": t.get("aiConfidence"),
            "pmoEdited": bool(t.get("pmoEdited", False)),
            "workforceSourcing": t.get("workforceSourcing"),
            "reviewPath": t.get("reviewPath"),
        })

    deps_in = [
        {
            "fromTaskKey": task_id_to_key[d["fromTaskId"]],
            "toTaskKey": task_id_to_key[d["toTaskId"]],
            "type": d.get("type", "finish_to_start"),
        }
        for d in source.get("dependencies", [])
        if d["fromTaskId"] in task_id_to_key and d["toTaskId"] in task_id_to_key
    ]

    _write_structure(new_plan_id, tenant_id, {
        "milestones": milestones_in,
        "tasks": tasks_in,
        "dependencies": deps_in,
    }, conn)
    conn.commit()

    plan = _get_plan_detail(new_plan_id)
    _audit(user, "decomposition.plan.copy", new_plan_id, extra={
        "sourcePlanId": plan_id,
        "sowId": sow_id,
        "newVersion": next_version,
        "milestoneCount": len(plan.get("milestones", [])) if plan else 0,
        "taskCount": len(plan.get("tasks", [])) if plan else 0,
    })
    return {"plan": plan}


# ── audit helper ──────────────────────────────────────────────────────────────


def _audit(user: dict, action: str, target_id: str, extra: dict | None = None) -> None:
    try:
        write_audit(
            actor_id=user.get("id"),
            actor_email=user.get("email"),
            actor_role=user.get("role"),
            action=action,
            target="decomposition_plan",
            target_id=target_id,
            service="enterprise",
            tenant_id=user.get("tenant_id"),
            extra=extra,
        )
    except Exception:  # noqa: BLE001
        pass
