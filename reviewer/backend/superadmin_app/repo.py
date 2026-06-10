"""Data-access helpers for the superadmin service (psycopg2 RealDict).

Covers login_accounts (user create / reviewer list / resend), contributor_pricing
(read/write JSONB), and the reviewer_assignments / reviewer_recommendations tables.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg2.extras import Json, RealDictCursor

from shared.db import ensure_pg_clean, get_pg_connection


def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def _user_out(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a login_accounts row into the camelCase shape the frontend uses."""
    if not row:
        return {}
    return {
        "id": str(row["id"]),
        "email": row.get("email"),
        "firstName": row.get("first_name") or "",
        "lastName": row.get("last_name") or "",
        "name": row.get("name") or f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip(),
        "role": row.get("role"),
        "phone": row.get("phone"),
        "department": row.get("department"),
        "tenantId": row.get("tenant_id"),
        "isActive": row.get("is_active", True),
        "mustChangePassword": row.get("must_change_password", False),
        "createdAt": row.get("created_at").isoformat() if row.get("created_at") else None,
        "lastLoginAt": row.get("last_login_at").isoformat() if row.get("last_login_at") else None,
    }


# ── login_accounts ───────────────────────────────────────────────────────────

def find_account_by_email(email: str) -> dict[str, Any] | None:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM login_accounts WHERE LOWER(email) = LOWER(%s)", (email,))
        return cur.fetchone()


def find_account_by_id(account_id: str) -> dict[str, Any] | None:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM login_accounts WHERE id = %s", (account_id,))
        return cur.fetchone()


def existing_emails_lower() -> set[str]:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute("SELECT LOWER(email) FROM login_accounts")
        return {r[0] for r in cur.fetchall()}


def create_account(
    *,
    email: str,
    password_hash: str | None,
    first_name: str,
    last_name: str,
    role: str,
    phone: str | None = None,
    department: str | None = None,
    provider: str = "password",
    tenant_id: str | None = None,
    must_change_password: bool = True,
) -> dict[str, Any]:
    conn = _conn()
    name = f"{first_name} {last_name}".strip()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO login_accounts
                (email, password_hash, provider, first_name, last_name, name, role,
                 phone, department, tenant_id, is_password_set, must_change_password)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
            """,
            (email.lower(), password_hash, provider, first_name, last_name, name, role,
             phone, department, tenant_id, password_hash is not None, must_change_password),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def link_tenant(account_id: str, tenant_id: str) -> None:
    """Backfill the tenant_id on an account that was created before its tenant
    link existed (e.g. self-registered, then provisioned). Idempotent."""
    if not tenant_id:
        return
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE login_accounts SET tenant_id = %s, updated_at = now() WHERE id = %s",
            (tenant_id, account_id),
        )
    conn.commit()


def set_temp_password(account_id: str, password_hash: str) -> None:
    """Set a (re)generated temporary password; force a change on next login."""
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE login_accounts
               SET password_hash = %s, is_password_set = TRUE,
                   must_change_password = TRUE, updated_at = now()
             WHERE id = %s
            """,
            (password_hash, account_id),
        )
    conn.commit()


# ── Approval queue (women freelancers self-apply → Super Admin approves) ──────

def list_applications(status: str = "pending") -> list[dict[str, Any]]:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, email, first_name, last_name, role, phone, tenant_id,
                   approval_status, created_at
              FROM login_accounts
             WHERE approval_status = %s
             ORDER BY created_at DESC
            """,
            (status,),
        )
        return cur.fetchall()


def set_approval_status(account_id: str, status: str) -> dict[str, Any] | None:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "UPDATE login_accounts SET approval_status = %s, updated_at = now() WHERE id = %s RETURNING *",
            (status, account_id),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def list_reviewers() -> list[dict[str, Any]]:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM login_accounts WHERE LOWER(role) = 'reviewer' ORDER BY created_at DESC"
        )
        return [_user_out(r) for r in cur.fetchall()]


def user_out(row: dict[str, Any]) -> dict[str, Any]:
    return _user_out(row)


# ── contributor_pricing (JSONB singleton) ────────────────────────────────────

def get_contributor_pricing() -> dict[str, Any] | None:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT data FROM contributor_pricing WHERE id = 1")
        row = cur.fetchone()
    if not row:
        return None
    data = row.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            data = None
    # Treat the empty-init {} as "not yet configured".
    if not data:
        return None
    return data


def set_contributor_pricing(data: dict[str, Any]) -> dict[str, Any]:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO contributor_pricing (id, data, updated_at)
            VALUES (1, %s, now())
            ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()
            """,
            (Json(data),),
        )
    conn.commit()
    return data


# ── reviewer_assignments ─────────────────────────────────────────────────────

def list_assignments_for_reviewer(reviewer_id: str | None, reviewer_email: str | None) -> list[dict[str, Any]]:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM reviewer_assignments
             WHERE (%s::bigint IS NOT NULL AND reviewer_id = %s::bigint)
                OR (%s IS NOT NULL AND LOWER(reviewer_email) = LOWER(%s))
                OR reviewer_id IS NULL  -- pool: handed off from mentor, unclaimed
             ORDER BY created_at DESC
            """,
            (reviewer_id, reviewer_id, reviewer_email, reviewer_email),
        )
        return [_assignment_out(r) for r in cur.fetchall()]


def get_assignment(assignment_id: str) -> dict[str, Any] | None:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM reviewer_assignments WHERE id = %s", (assignment_id,))
        return cur.fetchone()


def update_assignment(assignment_id: str, *, status: str | None = None,
                      priority: str | None = None, data: dict | None = None,
                      reviewer_id: Any | None = None, reviewer_email: str | None = None) -> dict[str, Any] | None:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE reviewer_assignments
               SET status   = COALESCE(%s, status),
                   priority = COALESCE(%s, priority),
                   data     = CASE WHEN %s::jsonb IS NOT NULL THEN data || %s::jsonb ELSE data END,
                   -- claim a pool (NULL) assignment to this reviewer on first action
                   reviewer_id    = COALESCE(reviewer_id, %s::bigint),
                   reviewer_email = COALESCE(reviewer_email, %s),
                   updated_at = now()
             WHERE id = %s
             RETURNING *
            """,
            (status, priority,
             Json(data) if data is not None else None,
             Json(data) if data is not None else None,
             reviewer_id, reviewer_email,
             assignment_id),
        )
        row = cur.fetchone()
    conn.commit()
    return _assignment_out(row) if row else None


def _assignment_out(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    data = row.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            data = {}
    return {
        "id": str(row["id"]),
        "reviewerId": str(row["reviewer_id"]) if row.get("reviewer_id") else None,
        "reviewerEmail": row.get("reviewer_email"),
        "projectId": row.get("project_id"),
        "projectName": row.get("project_name"),
        "title": row.get("title"),
        "status": row.get("status"),
        "priority": row.get("priority"),
        "data": data or {},
        "createdAt": row.get("created_at").isoformat() if row.get("created_at") else None,
        "updatedAt": row.get("updated_at").isoformat() if row.get("updated_at") else None,
    }


def assignment_out(row: dict[str, Any] | None) -> dict[str, Any]:
    return _assignment_out(row)


def reviewer_assignment_counts(reviewer_id: str | None, reviewer_email: str | None) -> dict[str, int]:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT status, COUNT(*) AS n FROM reviewer_assignments
             WHERE (%s::bigint IS NOT NULL AND reviewer_id = %s::bigint)
                OR (%s IS NOT NULL AND LOWER(reviewer_email) = LOWER(%s))
                OR reviewer_id IS NULL
             GROUP BY status
            """,
            (reviewer_id, reviewer_id, reviewer_email, reviewer_email),
        )
        rows = cur.fetchall()
    counts = {r["status"]: int(r["n"]) for r in rows}
    counts["total"] = sum(counts.values())
    return counts


# ── reviewer_recommendations ─────────────────────────────────────────────────

def create_recommendation(
    *,
    evidence_id: str,
    assignment_id: str | None,
    reviewer_id: str | None,
    reviewer_email: str | None,
    recommendation: str | None,
    score: float | None,
    comment: str | None,
    data: dict | None,
) -> dict[str, Any]:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO reviewer_recommendations
                (evidence_id, assignment_id, reviewer_id, reviewer_email,
                 recommendation, score, comment, data)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
            """,
            (evidence_id, assignment_id, reviewer_id, reviewer_email,
             recommendation, score, comment, Json(data or {})),
        )
        row = cur.fetchone()
    conn.commit()
    return _recommendation_out(row)


def _recommendation_out(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    data = row.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            data = {}
    return {
        "id": str(row["id"]),
        "evidenceId": row.get("evidence_id"),
        "assignmentId": str(row["assignment_id"]) if row.get("assignment_id") else None,
        "reviewerId": str(row["reviewer_id"]) if row.get("reviewer_id") else None,
        "reviewerEmail": row.get("reviewer_email"),
        "recommendation": row.get("recommendation"),
        "score": float(row["score"]) if row.get("score") is not None else None,
        "comment": row.get("comment"),
        "data": data or {},
        "createdAt": row.get("created_at").isoformat() if row.get("created_at") else None,
    }


def bulk_insert_accounts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert valid rows ON CONFLICT(email) DO NOTHING with must_change_password=TRUE.
    Each input row carries: email, firstName, lastName, name, role, phone, department,
    and `_password_hash`. Returns the rows actually inserted (with their temp password)."""
    inserted: list[dict[str, Any]] = []
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO login_accounts
                    (email, password_hash, provider, first_name, last_name, name,
                     role, phone, department, is_password_set, must_change_password)
                VALUES (%s,%s,'password',%s,%s,%s,%s,%s,%s,TRUE,TRUE)
                ON CONFLICT (email) DO NOTHING
                RETURNING *
                """,
                (
                    r["email"].lower(), r["_password_hash"], r.get("firstName") or "",
                    r.get("lastName") or "", r.get("name") or "",
                    (r.get("role") or "student").lower(), r.get("phone"), r.get("department"),
                ),
            )
            row = cur.fetchone()
            if row:
                out = _user_out(row)
                out["_temp_password"] = r.get("_temp_password")
                inserted.append(out)
    conn.commit()
    return inserted
