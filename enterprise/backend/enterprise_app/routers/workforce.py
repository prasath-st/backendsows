"""
Enterprise workforce directory + task assignment.

Endpoints:
  GET  /api/v1/enterprise/workforce
       List internal workforce members (search, department, limit, offset).
  POST /api/v1/enterprise/workforce
       Add one internal employee manually.
  POST /api/v1/enterprise/tasks/{taskId}/assign
       Assign a task to a contributor (creates contributor_tasks row).
  POST /api/v1/enterprise/tasks/{taskId}/reassign
       Reassign a previously-assigned task (reason + timestamps).
  POST /api/v1/enterprise/workforce/import/preview
       Parse a CSV body, diff against existing members, return {diffs, errors}.
  POST /api/v1/enterprise/workforce/import/apply
       Apply a previously-previewed (or directly supplied) CSV body; creates /
       updates / deactivates workforce members, returns counts.

Tables added (idempotent):
  enterprise_workforce_members
  enterprise_workforce_import_previews
  enterprise_task_assignments
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

router = APIRouter(prefix="/api/v1/enterprise", tags=["workforce"])


# ── helpers ───────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "") -> str:
    base = uuid.uuid4().hex[:24]
    return f"{prefix}{base}" if prefix else base


def _conn():
    ensure_pg_clean()
    return get_pg_connection()


# ── DDL (idempotent) ──────────────────────────────────────────────────────────

WORKFORCE_DDL = """
CREATE TABLE IF NOT EXISTS enterprise_workforce_members (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    email           TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    first_name      TEXT NOT NULL DEFAULT '',
    last_name       TEXT NOT NULL DEFAULT '',
    department      TEXT,
    employee_id     TEXT,
    primary_skills  JSONB NOT NULL DEFAULT '[]'::jsonb,
    availability    TEXT,
    manager_email   TEXT,
    cost_center     TEXT,
    contrib_type    TEXT NOT NULL DEFAULT 'internal',
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, email)
);
CREATE INDEX IF NOT EXISTS idx_wf_members_tenant ON enterprise_workforce_members(tenant_id);
CREATE INDEX IF NOT EXISTS idx_wf_members_email  ON enterprise_workforce_members(email);

CREATE TABLE IF NOT EXISTS enterprise_workforce_import_previews (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    csv_text    TEXT NOT NULL,
    diffs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    errors      JSONB NOT NULL DEFAULT '[]'::jsonb,
    valid_rows  JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wf_preview_tenant ON enterprise_workforce_import_previews(tenant_id);

CREATE TABLE IF NOT EXISTS enterprise_task_assignments (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    task_id             TEXT NOT NULL,
    plan_id             TEXT,
    assignee_id         TEXT,
    assignee_email      TEXT,
    assignee_name       TEXT,
    assigned_by_id      TEXT NOT NULL,
    assigned_by_email   TEXT,
    assigned_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    reassigned_at       TIMESTAMPTZ,
    reason              TEXT,
    prev_assignee_id    TEXT,
    prev_assignee_email TEXT,
    status              TEXT NOT NULL DEFAULT 'assigned',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ent_task_assign_tenant  ON enterprise_task_assignments(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ent_task_assign_task    ON enterprise_task_assignments(task_id);
CREATE INDEX IF NOT EXISTS idx_ent_task_assign_assignee ON enterprise_task_assignments(assignee_id);
"""


def init_workforce_schema() -> None:
    """Create tables idempotently. Called from enterprise_app.main._startup."""
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(WORKFORCE_DDL)
    conn.commit()


# ── CSV parsing helpers (no external deps) ────────────────────────────────────

_HEADER_ALIASES: dict[str, str] = {
    "employee_id": "employeeId", "employeeid": "employeeId", "emp_id": "employeeId",
    "email": "email", "work_email": "email",
    "first_name": "firstName", "firstname": "firstName", "given_name": "firstName",
    "last_name": "lastName", "lastname": "lastName", "family_name": "lastName",
    "department": "department", "org": "department", "organization": "department", "dept": "department",
    "primary_skills": "primarySkills", "skills": "primarySkills",
    "manager_email": "managerEmail", "manager": "managerEmail",
    "cost_center": "costCenter", "costcenter": "costCenter",
    "status": "status",
}


def _parse_csv_line(line: str) -> list[str]:
    out: list[str] = []
    cur = ""
    in_quotes = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            if in_quotes and i + 1 < len(line) and line[i + 1] == '"':
                cur += '"'
                i += 1
            else:
                in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    out.append(cur.strip())
    return out


def _split_skills(raw: str) -> list[str]:
    if not raw.strip():
        return []
    import re
    return [s.strip() for s in re.split(r"[,;|]", raw) if s.strip()]


def _parse_status(raw: str) -> str:
    v = raw.strip().lower()
    if v in ("inactive", "terminated", "no"):
        return "inactive"
    return "active"


def _parse_csv(csv_text: str) -> tuple[list[dict[str, str]], list[dict]]:
    """Return (raw_rows, parse_errors). raw_rows use canonical key names."""
    errors: list[dict] = []
    text = csv_text.lstrip("﻿").strip()
    if not text:
        return [], [{"row": 0, "message": "CSV file is empty."}]
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if len(lines) < 2:
        return [], [{"row": 0, "message": "CSV must include a header row and at least one data row."}]

    raw_headers = _parse_csv_line(lines[0])
    col_map: list[tuple[str, int]] = []
    for idx, h in enumerate(raw_headers):
        norm = h.strip().lower().replace(" ", "_")
        key = _HEADER_ALIASES.get(norm)
        if key:
            col_map.append((key, idx))

    if not any(k == "email" for k, _ in col_map):
        return [], [{"row": 1, "message": "Missing required column: email"}]

    rows: list[dict[str, str]] = []
    for li, line in enumerate(lines[1:], start=2):
        cells = _parse_csv_line(line)
        record: dict[str, str] = {}
        for key, idx in col_map:
            record[key] = cells[idx] if idx < len(cells) else ""
        rows.append(record)

    return rows, errors


def _normalize_row(raw: dict[str, str], row_number: int) -> tuple[dict | None, dict | None]:
    email = raw.get("email", "").strip().lower()
    if not email or "@" not in email:
        return None, {"row": row_number, "email": email, "message": "Valid email is required."}
    first_name = raw.get("firstName", "").strip()
    if not first_name:
        return None, {"row": row_number, "email": email, "message": "first_name is required."}
    department = raw.get("department", "").strip()
    if not department:
        return None, {"row": row_number, "email": email, "message": "department is required."}
    return {
        "employeeId": raw.get("employeeId", "").strip() or None,
        "email": email,
        "firstName": first_name,
        "lastName": raw.get("lastName", "").strip(),
        "department": department,
        "primarySkills": _split_skills(raw.get("primarySkills", "")),
        "managerEmail": raw.get("managerEmail", "").strip().lower() or None,
        "costCenter": raw.get("costCenter", "").strip() or None,
        "status": _parse_status(raw.get("status", "active")),
    }, None


def _compute_diffs(valid_rows: list[dict], existing_by_email: dict[str, dict]) -> list[dict]:
    diffs: list[dict] = []
    for row in valid_rows:
        email = row["email"]
        existing = existing_by_email.get(email)
        name = f"{row['firstName']} {row['lastName']}".strip()
        manager = row.get("managerEmail") or "—"
        cost_center = row.get("costCenter") or "—"
        status = row.get("status", "active")

        if not existing:
            if status == "inactive":
                continue
            diffs.append({"email": email, "name": name, "role": "contributor",
                          "manager": manager, "costCenter": cost_center, "action": "create"})
        else:
            if status == "inactive":
                if existing.get("status") != "inactive":
                    diffs.append({"email": email, "name": name, "role": "contributor",
                                  "manager": manager, "costCenter": cost_center, "action": "deactivate"})
            else:
                changed = (
                    existing.get("first_name") != row["firstName"]
                    or existing.get("last_name") != row["lastName"]
                    or existing.get("department") != row["department"]
                    or existing.get("status") == "inactive"
                )
                if changed:
                    diffs.append({"email": email, "name": name, "role": "contributor",
                                  "manager": manager, "costCenter": cost_center, "action": "update"})
    return diffs


# ── GET /api/v1/enterprise/workforce ─────────────────────────────────────────


@router.get("/workforce")
def list_workforce(
    user: Annotated[dict, Depends(get_current_user)],
    search: str | None = Query(default=None),
    department: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Return internal workforce members for this tenant."""
    tenant_id = user.get("tenant_id") or user.get("id") or "default"
    conn = _conn()
    clauses = ["tenant_id = %s"]
    params: list[Any] = [tenant_id]

    if search:
        needle = f"%{search.strip().lower()}%"
        clauses.append(
            "(LOWER(email) LIKE %s OR LOWER(display_name) LIKE %s OR LOWER(employee_id) LIKE %s)"
        )
        params += [needle, needle, needle]

    if department:
        clauses.append("LOWER(department) LIKE %s")
        params.append(f"%{department.strip().lower()}%")

    where = " AND ".join(clauses)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT COUNT(*) as cnt FROM enterprise_workforce_members WHERE {where}", params)
        total = cur.fetchone()["cnt"]

        cur.execute(
            f"""SELECT id, email, display_name, department, employee_id,
                       primary_skills, availability, manager_email, cost_center,
                       contrib_type, status, first_name, last_name
                FROM enterprise_workforce_members
                WHERE {where}
                ORDER BY display_name ASC
                LIMIT %s OFFSET %s""",
            params + [limit, offset],
        )
        rows = cur.fetchall()

    items = [
        {
            "userId": r["id"],
            "email": r["email"],
            "displayName": r["display_name"],
            "department": r["department"],
            "contribType": r["contrib_type"],
            "primarySkills": r["primary_skills"] or [],
            "availability": r["availability"],
            "employeeId": r["employee_id"],
            "managerEmail": r["manager_email"],
            "costCenter": r["cost_center"],
            "status": r["status"],
        }
        for r in rows
    ]

    return {"items": items, "total": total}


# ── POST /api/v1/enterprise/workforce (add one employee manually) ─────────────


@router.post("/workforce")
def add_workforce_member(
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
):
    """Add / update one internal employee manually."""
    tenant_id = user.get("tenant_id") or user.get("id") or "default"
    first_name = (body.get("firstName") or "").strip()
    last_name = (body.get("lastName") or "").strip()
    email = (body.get("email") or "").strip().lower()
    department = (body.get("department") or "").strip()

    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Valid email is required.")
    if not first_name:
        raise HTTPException(status_code=422, detail="firstName is required.")
    if not department:
        raise HTTPException(status_code=422, detail="department is required.")

    display_name = f"{first_name} {last_name}".strip()
    employee_id = (body.get("employeeId") or "").strip() or None
    primary_skills = _split_skills(body.get("primarySkills") or "")
    manager_email = (body.get("managerEmail") or "").strip().lower() or None
    cost_center = (body.get("costCenter") or "").strip() or None

    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, status FROM enterprise_workforce_members WHERE tenant_id = %s AND email = %s",
            [tenant_id, email],
        )
        existing = cur.fetchone()

    if existing:
        member_id = existing["id"]
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """UPDATE enterprise_workforce_members
                   SET display_name=%s, first_name=%s, last_name=%s, department=%s,
                       employee_id=COALESCE(%s, employee_id),
                       primary_skills=%s, manager_email=%s, cost_center=%s,
                       status='active', updated_at=now()
                   WHERE id=%s
                   RETURNING *""",
                [display_name, first_name, last_name, department, employee_id,
                 Json(primary_skills), manager_email, cost_center, member_id],
            )
            row = cur.fetchone()
        conn.commit()
        created = False
    else:
        member_id = _new_id("wf_")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO enterprise_workforce_members
                   (id, tenant_id, email, display_name, first_name, last_name, department,
                    employee_id, primary_skills, manager_email, cost_center, contrib_type, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'internal','active')
                   RETURNING *""",
                [member_id, tenant_id, email, display_name, first_name, last_name, department,
                 employee_id, Json(primary_skills), manager_email, cost_center],
            )
            row = cur.fetchone()
        conn.commit()
        created = True

    write_audit(
        actor_id=user.get("id"), actor_email=user.get("email"), actor_role=user.get("role"),
        action="workforce.member.add" if created else "workforce.member.update",
        target="enterprise_workforce_members", target_id=member_id,
        tenant_id=tenant_id,
        extra={"email": email, "department": department},
    )

    member = {
        "userId": row["id"],
        "email": row["email"],
        "displayName": row["display_name"],
        "department": row["department"],
        "contribType": row["contrib_type"],
        "primarySkills": row["primary_skills"] or [],
        "availability": row["availability"],
        "employeeId": row["employee_id"],
        "managerEmail": row["manager_email"],
        "costCenter": row["cost_center"],
        "status": row["status"],
    }
    return {"member": member, "created": created}


# ── POST /api/v1/enterprise/workforce/import/preview ─────────────────────────


@router.post("/workforce/import/preview")
def preview_workforce_import(
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
):
    """Parse CSV text, diff against DB, return {diffs, errors, previewId}.

    Body shape: { csv: string }   — raw CSV text.
    """
    tenant_id = user.get("tenant_id") or user.get("id") or "default"
    csv_text: str = (body.get("csv") or "").strip()
    if not csv_text:
        raise HTTPException(status_code=422, detail="csv field is required.")

    raw_rows, parse_errors = _parse_csv(csv_text)
    errors: list[dict] = list(parse_errors)
    valid_rows: list[dict] = []

    seen_emails: set[str] = set()
    for idx, raw in enumerate(raw_rows, start=2):
        row, err = _normalize_row(raw, idx)
        if err:
            errors.append(err)
        elif row:
            if row["email"] in seen_emails:
                errors.append({"row": idx, "email": row["email"],
                                "message": f"Duplicate email in CSV: {row['email']}"})
            else:
                seen_emails.add(row["email"])
                valid_rows.append(row)

    # Fetch existing members for diff
    conn = _conn()
    existing_by_email: dict[str, dict] = {}
    if valid_rows:
        emails = [r["email"] for r in valid_rows]
        placeholders = ",".join(["%s"] * len(emails))
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT email, first_name, last_name, department, status "
                f"FROM enterprise_workforce_members WHERE tenant_id=%s AND email IN ({placeholders})",
                [tenant_id] + emails,
            )
            for r in cur.fetchall():
                existing_by_email[r["email"]] = dict(r)

    diffs = _compute_diffs(valid_rows, existing_by_email) if valid_rows else []

    # Persist preview so /apply can reference it
    preview_id = _new_id("prev_")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO enterprise_workforce_import_previews
               (id, tenant_id, created_by, csv_text, diffs, errors, valid_rows)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            [preview_id, tenant_id, user.get("id") or "", csv_text,
             Json(diffs), Json(errors), Json(valid_rows)],
        )
    conn.commit()

    return {"diffs": diffs, "errors": errors, "previewId": preview_id}


# ── POST /api/v1/enterprise/workforce/import/apply ───────────────────────────


@router.post("/workforce/import/apply")
def apply_workforce_import(
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
):
    """Apply workforce CSV import. Accepts either:
      { csv: string }                — re-parses and applies
      { previewId: string }          — applies the stored preview
    Returns { success, applied, created, updated, deactivated, diffs }.
    """
    tenant_id = user.get("tenant_id") or user.get("id") or "default"
    conn = _conn()

    preview_id: str | None = body.get("previewId")
    csv_text: str = ""
    valid_rows: list[dict] = []
    diffs: list[dict] = []

    if preview_id:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM enterprise_workforce_import_previews WHERE id=%s AND tenant_id=%s",
                [preview_id, tenant_id],
            )
            row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Preview not found or expired.")
        csv_text = row["csv_text"]
        valid_rows = row["valid_rows"] or []
        diffs = row["diffs"] or []
        errors: list[dict] = row["errors"] or []
        if errors and not valid_rows:
            raise HTTPException(status_code=422, detail=errors[0].get("message", "CSV has errors."))
    else:
        csv_text = (body.get("csv") or "").strip()
        if not csv_text:
            raise HTTPException(status_code=422, detail="csv or previewId is required.")
        raw_rows, parse_errors = _parse_csv(csv_text)
        errors = list(parse_errors)
        seen_emails: set[str] = set()
        for idx, raw in enumerate(raw_rows, start=2):
            row_data, err = _normalize_row(raw, idx)
            if err:
                errors.append(err)
            elif row_data:
                if row_data["email"] not in seen_emails:
                    seen_emails.add(row_data["email"])
                    valid_rows.append(row_data)
        if errors and not valid_rows:
            raise HTTPException(status_code=422, detail=errors[0].get("message", "CSV has errors."))

        emails = [r["email"] for r in valid_rows]
        existing_by_email: dict[str, dict] = {}
        if emails:
            placeholders = ",".join(["%s"] * len(emails))
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"SELECT email, first_name, last_name, department, status "
                    f"FROM enterprise_workforce_members WHERE tenant_id=%s AND email IN ({placeholders})",
                    [tenant_id] + emails,
                )
                for r in cur.fetchall():
                    existing_by_email[r["email"]] = dict(r)
        diffs = _compute_diffs(valid_rows, existing_by_email)

    if not valid_rows:
        raise HTTPException(status_code=422, detail="No valid rows to apply.")

    created = updated = deactivated = 0

    for row in valid_rows:
        email = row["email"]
        status = row.get("status", "active")
        display_name = f"{row['firstName']} {row.get('lastName', '')}".strip()
        primary_skills = row.get("primarySkills") or []
        department = row.get("department", "")
        first_name = row.get("firstName", "")
        last_name = row.get("lastName", "")
        employee_id = row.get("employeeId")
        manager_email = row.get("managerEmail")
        cost_center = row.get("costCenter")

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, status FROM enterprise_workforce_members WHERE tenant_id=%s AND email=%s",
                [tenant_id, email],
            )
            existing = cur.fetchone()

        if status == "inactive":
            if existing:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE enterprise_workforce_members SET status='inactive', updated_at=now() WHERE id=%s",
                        [existing["id"]],
                    )
                conn.commit()
                deactivated += 1
            continue

        if existing:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE enterprise_workforce_members
                       SET display_name=%s, first_name=%s, last_name=%s, department=%s,
                           employee_id=COALESCE(%s, employee_id),
                           primary_skills=%s, manager_email=%s, cost_center=%s,
                           status='active', updated_at=now()
                       WHERE id=%s""",
                    [display_name, first_name, last_name, department, employee_id,
                     Json(primary_skills), manager_email, cost_center, existing["id"]],
                )
            conn.commit()
            updated += 1
        else:
            member_id = _new_id("wf_")
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO enterprise_workforce_members
                       (id, tenant_id, email, display_name, first_name, last_name, department,
                        employee_id, primary_skills, manager_email, cost_center, contrib_type, status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'internal','active')
                       ON CONFLICT (tenant_id, email) DO UPDATE
                         SET display_name=EXCLUDED.display_name,
                             first_name=EXCLUDED.first_name,
                             last_name=EXCLUDED.last_name,
                             department=EXCLUDED.department,
                             employee_id=COALESCE(EXCLUDED.employee_id, enterprise_workforce_members.employee_id),
                             primary_skills=EXCLUDED.primary_skills,
                             manager_email=EXCLUDED.manager_email,
                             cost_center=EXCLUDED.cost_center,
                             status='active', updated_at=now()""",
                    [member_id, tenant_id, email, display_name, first_name, last_name, department,
                     employee_id, Json(primary_skills), manager_email, cost_center],
                )
            conn.commit()
            created += 1

    applied = created + updated + deactivated

    write_audit(
        actor_id=user.get("id"), actor_email=user.get("email"), actor_role=user.get("role"),
        action="workforce.import.apply",
        tenant_id=tenant_id,
        extra={"applied": applied, "created": created, "updated": updated, "deactivated": deactivated},
    )

    return {
        "success": True,
        "applied": applied,
        "created": created,
        "updated": updated,
        "deactivated": deactivated,
        "diffs": diffs,
    }


# ── POST /api/v1/enterprise/tasks/{taskId}/assign ─────────────────────────────


@router.post("/tasks/{task_id}/assign")
def assign_task(
    task_id: str,
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
):
    """Assign a task to a contributor.

    Body shape:
      {
        contributorId?: string,
        contributorEmail?: string,
        contributorName?: string,
        planId?: string,
        reason?: string,
      }

    Also writes a contributor_tasks row so the task appears in their inbox.
    Returns { success, message, data: assignment }.
    """
    tenant_id = user.get("tenant_id") or user.get("id") or "default"
    assignee_id: str | None = (body.get("contributorId") or body.get("accountId") or "").strip() or None
    assignee_email: str | None = (body.get("contributorEmail") or "").strip().lower() or None
    assignee_name: str | None = (body.get("contributorName") or "").strip() or None
    plan_id: str | None = (body.get("planId") or "").strip() or None
    reason: str | None = (body.get("reason") or "").strip() or None

    if not assignee_id and not assignee_email:
        raise HTTPException(status_code=422, detail="contributorId or contributorEmail is required.")

    conn = _conn()

    # Resolve assignee_id from email if not provided
    if not assignee_id and assignee_email:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id FROM login_accounts WHERE email = %s LIMIT 1",
                [assignee_email],
            )
            row = cur.fetchone()
            if row:
                assignee_id = str(row["id"])

    assignment_id = _new_id("ta_")
    now = _now_iso()

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO enterprise_task_assignments
               (id, tenant_id, task_id, plan_id, assignee_id, assignee_email,
                assignee_name, assigned_by_id, assigned_by_email, assigned_at,
                reason, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'assigned')
               ON CONFLICT DO NOTHING""",
            [assignment_id, tenant_id, task_id, plan_id, assignee_id, assignee_email,
             assignee_name, user.get("id") or "", user.get("email"), now, reason],
        )
    conn.commit()

    # Update decomp_tasks if it exists there
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE decomp_tasks
               SET status='assigned', updated_at=now()
               WHERE id=%s""",
            [task_id],
        )
    conn.commit()

    # Create contributor_tasks row (best-effort)
    if assignee_id:
        try:
            _resolve_int = int(assignee_id)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT title FROM decomp_tasks WHERE id=%s LIMIT 1", [task_id]
                )
                task_row = cur.fetchone()
                title = task_row[0] if task_row else f"Task {task_id}"
            from psycopg2.extras import Json as _Json
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO contributor_tasks
                       (account_id, title, status, priority, category, data)
                       VALUES (%s,%s,'assigned','normal','delivery',%s)
                       ON CONFLICT DO NOTHING""",
                    [_resolve_int, title,
                     _Json({"assignmentId": assignment_id, "taskId": task_id,
                            "planId": plan_id, "reason": reason})],
                )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("contributor_tasks insert skipped: %s", exc)

    write_audit(
        actor_id=user.get("id"), actor_email=user.get("email"), actor_role=user.get("role"),
        action="task.assign",
        target="decomp_tasks", target_id=task_id,
        tenant_id=tenant_id,
        extra={"assigneeId": assignee_id, "assigneeEmail": assignee_email, "planId": plan_id},
    )

    assignment = {
        "id": assignment_id,
        "taskId": task_id,
        "planId": plan_id,
        "assigneeId": assignee_id,
        "assigneeEmail": assignee_email,
        "assigneeName": assignee_name,
        "assignedById": user.get("id"),
        "assignedByEmail": user.get("email"),
        "assignedAt": now,
        "reason": reason,
        "status": "assigned",
    }
    return {"success": True, "message": "Task assigned.", "data": assignment}


# ── POST /api/v1/enterprise/tasks/{taskId}/reassign ──────────────────────────


@router.post("/tasks/{task_id}/reassign")
def reassign_task(
    task_id: str,
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
):
    """Reassign a task to a different contributor.

    Body shape:
      {
        contributorId?: string,
        contributorEmail?: string,
        contributorName?: string,
        planId?: string,
        reason?: string,
      }

    Closes the previous assignment row, creates a new one with reassigned_at stamped.
    Returns { success, message, data: assignment }.
    """
    tenant_id = user.get("tenant_id") or user.get("id") or "default"
    new_assignee_id: str | None = (body.get("contributorId") or body.get("accountId") or "").strip() or None
    new_assignee_email: str | None = (body.get("contributorEmail") or "").strip().lower() or None
    new_assignee_name: str | None = (body.get("contributorName") or "").strip() or None
    plan_id: str | None = (body.get("planId") or "").strip() or None
    reason: str | None = (body.get("reason") or "").strip() or None

    if not new_assignee_id and not new_assignee_email:
        raise HTTPException(status_code=422, detail="contributorId or contributorEmail is required.")

    conn = _conn()

    # Resolve new_assignee_id from email if not provided
    if not new_assignee_id and new_assignee_email:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id FROM login_accounts WHERE email=%s LIMIT 1", [new_assignee_email])
            r = cur.fetchone()
            if r:
                new_assignee_id = str(r["id"])

    # Find the current active assignment (if any) for prev-assignee info
    prev_assignee_id: str | None = None
    prev_assignee_email: str | None = None
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """SELECT id, assignee_id, assignee_email
               FROM enterprise_task_assignments
               WHERE task_id=%s AND tenant_id=%s AND status='assigned'
               ORDER BY assigned_at DESC LIMIT 1""",
            [task_id, tenant_id],
        )
        prev = cur.fetchone()
    if prev:
        prev_assignee_id = prev["assignee_id"]
        prev_assignee_email = prev["assignee_email"]
        # Close the previous assignment
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE enterprise_task_assignments SET status='reassigned', updated_at=now() WHERE id=%s",
                [prev["id"]],
            )
        conn.commit()

    now = _now_iso()
    assignment_id = _new_id("ta_")

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO enterprise_task_assignments
               (id, tenant_id, task_id, plan_id, assignee_id, assignee_email,
                assignee_name, assigned_by_id, assigned_by_email, assigned_at,
                reassigned_at, reason, prev_assignee_id, prev_assignee_email, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'assigned')""",
            [assignment_id, tenant_id, task_id, plan_id, new_assignee_id, new_assignee_email,
             new_assignee_name, user.get("id") or "", user.get("email"), now,
             now, reason, prev_assignee_id, prev_assignee_email],
        )
    conn.commit()

    # Update decomp_tasks
    with conn.cursor() as cur:
        cur.execute("UPDATE decomp_tasks SET status='assigned', updated_at=now() WHERE id=%s", [task_id])
    conn.commit()

    # Create contributor_tasks for new assignee (best-effort)
    if new_assignee_id:
        try:
            _resolve_int = int(new_assignee_id)
            with conn.cursor() as cur:
                cur.execute("SELECT title FROM decomp_tasks WHERE id=%s LIMIT 1", [task_id])
                task_row = cur.fetchone()
                title = task_row[0] if task_row else f"Task {task_id}"
            from psycopg2.extras import Json as _Json
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO contributor_tasks
                       (account_id, title, status, priority, category, data)
                       VALUES (%s,%s,'assigned','normal','delivery',%s)
                       ON CONFLICT DO NOTHING""",
                    [_resolve_int, title,
                     _Json({"assignmentId": assignment_id, "taskId": task_id,
                            "planId": plan_id, "reason": reason, "isReassign": True})],
                )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("contributor_tasks reassign insert skipped: %s", exc)

    write_audit(
        actor_id=user.get("id"), actor_email=user.get("email"), actor_role=user.get("role"),
        action="task.reassign",
        target="decomp_tasks", target_id=task_id,
        tenant_id=tenant_id,
        extra={
            "newAssigneeId": new_assignee_id,
            "newAssigneeEmail": new_assignee_email,
            "prevAssigneeId": prev_assignee_id,
            "prevAssigneeEmail": prev_assignee_email,
            "reason": reason,
        },
    )

    assignment = {
        "id": assignment_id,
        "taskId": task_id,
        "planId": plan_id,
        "assigneeId": new_assignee_id,
        "assigneeEmail": new_assignee_email,
        "assigneeName": new_assignee_name,
        "assignedById": user.get("id"),
        "assignedByEmail": user.get("email"),
        "assignedAt": now,
        "reassignedAt": now,
        "prevAssigneeId": prev_assignee_id,
        "prevAssigneeEmail": prev_assignee_email,
        "reason": reason,
        "status": "assigned",
    }
    return {"success": True, "message": "Task reassigned.", "data": assignment}
