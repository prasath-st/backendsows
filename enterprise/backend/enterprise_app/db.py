"""
Enterprise-service persistence layer.

Everything the frontend sends (wizards, SOWs, decomposition plans, tasks, …)
is nested/document-shaped, so we store each as a single JSONB ``data`` column
keyed by owner (the authenticated user) + a generated id. Generic sub-resource
reads slice into that JSONB; writes merge back into it.

All tables live in Postgres (Neon) and are created idempotently on startup.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg2.extras import Json, RealDictCursor

from shared.db import ensure_pg_clean, get_pg_connection

logger = logging.getLogger(__name__)


# ── schema ────────────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS enterprise_wizards (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    owner_email TEXT,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ent_wizards_owner ON enterprise_wizards(owner_id);

CREATE TABLE IF NOT EXISTS enterprise_sows (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    owner_email TEXT,
    source      TEXT NOT NULL DEFAULT 'ai',   -- 'ai' | 'manual'
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ent_sows_owner ON enterprise_sows(owner_id);

CREATE TABLE IF NOT EXISTS enterprise_plans (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    owner_email TEXT,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ent_plans_owner ON enterprise_plans(owner_id);

CREATE TABLE IF NOT EXISTS enterprise_projects (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    owner_email TEXT,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ent_projects_owner ON enterprise_projects(owner_id);

CREATE TABLE IF NOT EXISTS enterprise_profiles_ext (
    owner_id    TEXT PRIMARY KEY,
    owner_email TEXT,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    picture_url TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS enterprise_invoices (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    owner_email TEXT,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ent_invoices_owner ON enterprise_invoices(owner_id);

CREATE TABLE IF NOT EXISTS enterprise_deliverables (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    owner_email TEXT,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ent_deliverables_owner ON enterprise_deliverables(owner_id);

-- ── Decomposition plans (normalised) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS decomp_plans (
    id                          TEXT PRIMARY KEY,
    tenant_id                   TEXT NOT NULL,
    sow_id                      TEXT NOT NULL,
    version                     INTEGER NOT NULL DEFAULT 1,
    status                      TEXT NOT NULL DEFAULT 'draft',
    summary                     TEXT,
    source_agent_invocation_id  TEXT,
    default_workforce_sourcing  TEXT,
    default_review_path         TEXT,
    two_stage_review_enabled    BOOLEAN NOT NULL DEFAULT FALSE,
    approved_at                 TIMESTAMPTZ,
    approved_by                 TEXT,
    activated_at                TIMESTAMPTZ,
    archived_at                 TIMESTAMPTZ,
    created_by                  TEXT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at                  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_decomp_plans_tenant   ON decomp_plans(tenant_id);
CREATE INDEX IF NOT EXISTS idx_decomp_plans_sow      ON decomp_plans(sow_id);
CREATE INDEX IF NOT EXISTS idx_decomp_plans_status   ON decomp_plans(status);

CREATE TABLE IF NOT EXISTS decomp_milestones (
    id          TEXT PRIMARY KEY,
    plan_id     TEXT NOT NULL REFERENCES decomp_plans(id) ON DELETE CASCADE,
    tenant_id   TEXT NOT NULL,
    "order"     INTEGER NOT NULL DEFAULT 0,
    name        TEXT NOT NULL,
    description TEXT,
    start_date  TIMESTAMPTZ,
    end_date    TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decomp_ms_plan ON decomp_milestones(plan_id);

CREATE TABLE IF NOT EXISTS decomp_tasks (
    id                   TEXT PRIMARY KEY,
    plan_id              TEXT NOT NULL REFERENCES decomp_plans(id) ON DELETE CASCADE,
    milestone_id         TEXT REFERENCES decomp_milestones(id) ON DELETE SET NULL,
    tenant_id            TEXT NOT NULL,
    external_key         TEXT,
    title                TEXT NOT NULL,
    description          TEXT,
    required_skills      JSONB NOT NULL DEFAULT '[]'::jsonb,
    estimated_hours      NUMERIC(10,2),
    acceptance_criteria  TEXT,
    complexity           TEXT,
    "order"              INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'draft',
    ai_confidence        INTEGER,
    pmo_edited           BOOLEAN NOT NULL DEFAULT FALSE,
    workforce_sourcing   TEXT,
    review_path          TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decomp_tasks_plan      ON decomp_tasks(plan_id);
CREATE INDEX IF NOT EXISTS idx_decomp_tasks_milestone ON decomp_tasks(milestone_id);

CREATE TABLE IF NOT EXISTS decomp_dependencies (
    id            TEXT PRIMARY KEY,
    from_task_id  TEXT NOT NULL REFERENCES decomp_tasks(id) ON DELETE CASCADE,
    to_task_id    TEXT NOT NULL REFERENCES decomp_tasks(id) ON DELETE CASCADE,
    tenant_id     TEXT NOT NULL,
    type          TEXT NOT NULL DEFAULT 'finish_to_start',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_decomp_deps_from ON decomp_dependencies(from_task_id);
CREATE INDEX IF NOT EXISTS idx_decomp_deps_to   ON decomp_dependencies(to_task_id);

-- ── Enterprise review queue ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS enterprise_review_queue (
    submission_id   TEXT PRIMARY KEY,
    data            JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'pending',
    claimed_by      TEXT,
    claimed_at      TIMESTAMPTZ,
    decision        TEXT,
    decided_at      TIMESTAMPTZ,
    decided_by      TEXT,
    decision_note   TEXT,
    decision_id     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ent_rq_status  ON enterprise_review_queue(status);
CREATE INDEX IF NOT EXISTS idx_ent_rq_claimed ON enterprise_review_queue(claimed_by);
"""


def init_enterprise_schema() -> None:
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()
    # Also ensure the compliance/billing/razorpay tables
    try:
        from enterprise_app.routers.compliance_billing import (
            init_compliance_billing_schema,
        )
        init_compliance_billing_schema()
    except Exception as exc:  # noqa: BLE001
        logger.warning("compliance_billing schema init failed: %s", exc)
    # Workforce + task assignment tables
    try:
        from enterprise_app.routers.workforce import init_workforce_schema
        init_workforce_schema()
    except Exception as exc:  # noqa: BLE001
        logger.warning("workforce schema init failed: %s", exc)


# ── helpers ─────────────────────────────────────────────────────────────────--

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str = "") -> str:
    base = uuid.uuid4().hex[:24]
    return f"{prefix}{base}" if prefix else base


_TABLES = {
    "wizard": "enterprise_wizards",
    "sow": "enterprise_sows",
    "plan": "enterprise_plans",
    "project": "enterprise_projects",
    "invoice": "enterprise_invoices",
    "deliverable": "enterprise_deliverables",
}


# Canonical 5-stage SOW approval pipeline (Fayaz's flow):
#   Business (enterprise) → Commercial (Glimmora platform admin) → Legal →
#   Security → Final. Any stage can reject back to draft.
# Glimmora's Commercial gate is the VERY LAST step — it runs only after ALL the
# enterprise gates (Business/Legal/Security + the enterprise Final sign-off) have
# cleared. Glimmora closes the SOW.
SOW_APPROVAL_STAGES = [
    {"key": "legal",      "title": "Legal / Compliance Review",   "owner": "enterprise"},
    {"key": "finance",    "title": "Finance / Business Review",   "owner": "enterprise"},
    {"key": "security",   "title": "Security Review",             "owner": "enterprise"},
    {"key": "final",      "title": "Final Sign-off",              "owner": "enterprise"},
    {"key": "commercial", "title": "GlimmoraTeam Commercial Review", "owner": "platform"},
]


def build_approval_stages(*, all_status: str = "pending", decided_by: str | None = None) -> list[dict]:
    """Return the 5 canonical stages, each set to `all_status`. When approved,
    stamp decidedBy/decidedAt so the timeline reads correctly."""
    out: list[dict] = []
    for s in SOW_APPROVAL_STAGES:
        stage = {"key": s["key"], "title": s["title"], "owner": s["owner"], "status": all_status}
        if all_status == "approved":
            stage["decidedBy"] = decided_by or "ai"
            stage["decidedAt"] = now_iso()
        out.append(stage)
    return out


def _table(kind: str) -> str:
    return _TABLES[kind]


# ── generic JSONB CRUD ────────────────────────────────────────────────────────

def create_row(kind: str, owner: dict, data: dict, *, row_id: str | None = None,
               extra_cols: dict | None = None) -> dict:
    ensure_pg_clean()
    conn = get_pg_connection()
    rid = row_id or new_id()
    data = dict(data or {})
    data.setdefault("id", rid)
    cols = ["id", "owner_id", "owner_email", "data"]
    vals: list[Any] = [rid, owner.get("id"), owner.get("email"), Json(data)]
    for k, v in (extra_cols or {}).items():
        cols.append(k)
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"INSERT INTO {_table(kind)} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"RETURNING data",
            vals,
        )
        row = cur.fetchone()
    conn.commit()
    return row["data"]


def get_row(kind: str, row_id: str, owner_id: str | None = None) -> dict | None:
    ensure_pg_clean()
    conn = get_pg_connection()
    sql = f"SELECT data FROM {_table(kind)} WHERE id = %s"
    params: list[Any] = [row_id]
    if owner_id is not None:
        sql += " AND owner_id = %s"
        params.append(owner_id)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row["data"] if row else None


def list_rows(kind: str, owner_id: str | None = None, *, where_sql: str = "",
              params: list | None = None) -> list[dict]:
    ensure_pg_clean()
    conn = get_pg_connection()
    sql = f"SELECT data FROM {_table(kind)}"
    clauses = []
    p: list[Any] = []
    if owner_id is not None:
        clauses.append("owner_id = %s")
        p.append(owner_id)
    if where_sql:
        clauses.append(where_sql)
        p.extend(params or [])
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC"
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, p)
        rows = cur.fetchall()
    return [r["data"] for r in rows]


def update_row(kind: str, row_id: str, data: dict, owner_id: str | None = None) -> dict | None:
    ensure_pg_clean()
    conn = get_pg_connection()
    sql = f"UPDATE {_table(kind)} SET data = %s, updated_at = now() WHERE id = %s"
    params: list[Any] = [Json(data), row_id]
    if owner_id is not None:
        sql += " AND owner_id = %s"
        params.append(owner_id)
    sql += " RETURNING data"
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.commit()
    return row["data"] if row else None


def delete_row(kind: str, row_id: str, owner_id: str | None = None) -> bool:
    ensure_pg_clean()
    conn = get_pg_connection()
    sql = f"DELETE FROM {_table(kind)} WHERE id = %s"
    params: list[Any] = [row_id]
    if owner_id is not None:
        sql += " AND owner_id = %s"
        params.append(owner_id)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        deleted = cur.rowcount
    conn.commit()
    return deleted > 0


def merge_row(kind: str, row_id: str, patch: dict, owner_id: str | None = None) -> dict | None:
    """Read-modify-write a stored JSONB document with a shallow merge."""
    current = get_row(kind, row_id, owner_id)
    if current is None:
        return None
    current.update(patch or {})
    current["updatedAt"] = now_iso()
    return update_row(kind, row_id, current, owner_id)


# ── profile (single row per owner) ─────────────────────────────────────────────

def get_profile(owner_id: str) -> dict | None:
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT data, picture_url FROM enterprise_profiles_ext WHERE owner_id = %s",
            [owner_id],
        )
        row = cur.fetchone()
    if not row:
        return None
    data = dict(row["data"] or {})
    if row.get("picture_url"):
        data["pictureUrl"] = row["picture_url"]
    return data


def upsert_profile(owner: dict, data: dict, picture_url: str | None = None) -> dict:
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO enterprise_profiles_ext (owner_id, owner_email, data, picture_url, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (owner_id) DO UPDATE
              SET data = COALESCE(EXCLUDED.data, enterprise_profiles_ext.data),
                  owner_email = EXCLUDED.owner_email,
                  picture_url = COALESCE(EXCLUDED.picture_url, enterprise_profiles_ext.picture_url),
                  updated_at = now()
            RETURNING data, picture_url
            """,
            [owner.get("id"), owner.get("email"), Json(data or {}), picture_url],
        )
        row = cur.fetchone()
    conn.commit()
    out = dict(row["data"] or {})
    if row.get("picture_url"):
        out["pictureUrl"] = row["picture_url"]
    return out
