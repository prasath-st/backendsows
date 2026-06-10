"""
Contributor-service persistence layer.

Owns all contributor-specific Postgres tables (idempotent CREATE TABLE IF NOT
EXISTS DDL in `init_contributor_schema`) plus small data-access helpers used by
the routers. Nested / flexible payloads are stored as JSONB and echoed back.

Convention: every list endpoint seeds a couple of demo rows for the current
account on first read (keyed by account_id) so the dashboard is never empty.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from psycopg2.extras import Json, RealDictCursor

from shared.db import ensure_pg_clean, get_pg_connection

logger = logging.getLogger(__name__)


# ── connection helper ────────────────────────────────────────────────────────

def conn():
    ensure_pg_clean()
    return get_pg_connection()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── schema ───────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- One login-account row may exist already (shared init). We only add our own.

CREATE TABLE IF NOT EXISTS contributor_settings (
    account_id    BIGINT PRIMARY KEY,
    data          JSONB NOT NULL DEFAULT '{}',
    mfa_enabled   BOOLEAN NOT NULL DEFAULT FALSE,
    mfa_secret    TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS contributor_tasks (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'available',   -- available|assigned|in_progress|submitted|completed|declined
    priority      TEXT NOT NULL DEFAULT 'medium',
    category      TEXT,
    reward        NUMERIC DEFAULT 0,
    currency      TEXT DEFAULT 'USD',
    due_at        TIMESTAMPTZ,
    data          JSONB NOT NULL DEFAULT '{}',          -- full nested task payload (workroom, checklist, timeline...)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_tasks_acct ON contributor_tasks(account_id);

CREATE TABLE IF NOT EXISTS contributor_task_messages (
    id            BIGSERIAL PRIMARY KEY,
    task_id       BIGINT NOT NULL,
    account_id    BIGINT NOT NULL,
    author        TEXT NOT NULL DEFAULT 'contributor',
    body          TEXT NOT NULL DEFAULT '',
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_taskmsg_task ON contributor_task_messages(task_id);

CREATE TABLE IF NOT EXISTS contributor_submissions (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    task_id       BIGINT,
    status        TEXT NOT NULL DEFAULT 'submitted',    -- submitted|in_review|approved|changes_requested|rejected
    summary       TEXT,
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_subs_acct ON contributor_submissions(account_id);

CREATE TABLE IF NOT EXISTS contributor_uploads (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    task_id       BIGINT,
    filename      TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    content_type  TEXT,
    size_bytes    BIGINT DEFAULT 0,
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_uploads_task ON contributor_uploads(task_id);

CREATE TABLE IF NOT EXISTS contributor_notifications (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    body          TEXT,
    category      TEXT DEFAULT 'general',
    is_read       BOOLEAN NOT NULL DEFAULT FALSE,
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_notif_acct ON contributor_notifications(account_id);

CREATE TABLE IF NOT EXISTS contributor_earnings (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    task_id       BIGINT,
    amount        NUMERIC NOT NULL DEFAULT 0,
    currency      TEXT NOT NULL DEFAULT 'USD',
    status        TEXT NOT NULL DEFAULT 'pending',      -- pending|cleared|paid
    description   TEXT,
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_earn_acct ON contributor_earnings(account_id);

CREATE TABLE IF NOT EXISTS contributor_payouts (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    amount        NUMERIC NOT NULL DEFAULT 0,
    currency      TEXT NOT NULL DEFAULT 'USD',
    status        TEXT NOT NULL DEFAULT 'processing',   -- processing|paid|failed
    method        TEXT,
    reference     TEXT,
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_payouts_acct ON contributor_payouts(account_id);

CREATE TABLE IF NOT EXISTS contributor_payout_preferences (
    account_id    BIGINT PRIMARY KEY,
    data          JSONB NOT NULL DEFAULT '{}',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS contributor_kyc (
    account_id    BIGINT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'not_started',  -- not_started|pending|verified|rejected
    data          JSONB NOT NULL DEFAULT '{}',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS contributor_message_threads (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    subject       TEXT NOT NULL DEFAULT '',
    counterpart   TEXT,
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_threads_acct ON contributor_message_threads(account_id);

CREATE TABLE IF NOT EXISTS contributor_thread_messages (
    id            BIGSERIAL PRIMARY KEY,
    thread_id     BIGINT NOT NULL,
    account_id    BIGINT NOT NULL,
    author        TEXT NOT NULL DEFAULT 'contributor',
    body          TEXT NOT NULL DEFAULT '',
    rating        INT,
    is_read       BOOLEAN NOT NULL DEFAULT FALSE,
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_threadmsg_thr ON contributor_thread_messages(thread_id);

CREATE TABLE IF NOT EXISTS contributor_learning (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    provider      TEXT,
    status        TEXT NOT NULL DEFAULT 'recommended',  -- recommended|opened|dismissed
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_learn_acct ON contributor_learning(account_id);

CREATE TABLE IF NOT EXISTS contributor_support_tickets (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'ticket',       -- ticket|grievance|safety_report
    subject       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open',
    priority      TEXT DEFAULT 'normal',
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_tickets_acct ON contributor_support_tickets(account_id);

CREATE TABLE IF NOT EXISTS contributor_support_messages (
    id            BIGSERIAL PRIMARY KEY,
    ticket_id     BIGINT NOT NULL,
    account_id    BIGINT NOT NULL,
    author        TEXT NOT NULL DEFAULT 'contributor',
    body          TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_supmsg_ticket ON contributor_support_messages(ticket_id);

CREATE TABLE IF NOT EXISTS contributor_credentials (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    issuer        TEXT DEFAULT 'Glimmora',
    kind          TEXT DEFAULT 'certificate',
    status        TEXT NOT NULL DEFAULT 'issued',       -- issued|pending|revoked
    verification_code TEXT,
    data          JSONB NOT NULL DEFAULT '{}',
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_creds_acct ON contributor_credentials(account_id);

CREATE TABLE IF NOT EXISTS credential_shares (
    id            BIGSERIAL PRIMARY KEY,
    share_id      TEXT NOT NULL UNIQUE,
    credential_id BIGINT NOT NULL,
    account_id    BIGINT NOT NULL,
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_cred_shares_share ON credential_shares(share_id);

CREATE TABLE IF NOT EXISTS contributor_evidence (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    kind          TEXT DEFAULT 'link',
    url           TEXT,
    status        TEXT NOT NULL DEFAULT 'active',
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_evidence_acct ON contributor_evidence(account_id);

CREATE TABLE IF NOT EXISTS contributor_digital_twin (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    snapshot      JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_twin_acct ON contributor_digital_twin(account_id);

-- ── Profile-completion sections (required for contributor-umbrella personas) ──
-- Portfolio projects.
CREATE TABLE IF NOT EXISTS contributor_projects (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    role          TEXT,
    url           TEXT,
    skills        TEXT[] DEFAULT '{}',
    start_date    TEXT,
    end_date      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_projects_acct ON contributor_projects(account_id);

-- Work / internship experience.
CREATE TABLE IF NOT EXISTS contributor_experience (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'internship',  -- 'internship' | 'job' | 'volunteer'
    organization  TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT '',
    description   TEXT,
    location      TEXT,
    start_date    TEXT,
    end_date      TEXT,
    is_current    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_experience_acct ON contributor_experience(account_id);

-- Education + areas of expertise / certifications.
CREATE TABLE IF NOT EXISTS contributor_education (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL,
    institution   TEXT NOT NULL DEFAULT '',
    degree        TEXT,
    field         TEXT,
    grade         TEXT,
    start_year    TEXT,
    end_year      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_education_acct ON contributor_education(account_id);

-- Expertise areas + certifications stored on the profile as arrays/JSON.
ALTER TABLE contributor_profiles ADD COLUMN IF NOT EXISTS expertise_areas TEXT[] DEFAULT '{}';
ALTER TABLE contributor_profiles ADD COLUMN IF NOT EXISTS certifications  JSONB  DEFAULT '[]';
ALTER TABLE contributor_profiles ADD COLUMN IF NOT EXISTS profile_completed BOOLEAN NOT NULL DEFAULT FALSE;

-- ── Task-interest board (the "I'm interested" flow) ───────────────────────────
-- SHARED table: the freelancer service writes interest rows for decomposed
-- enterprise_plans tasks; the enterprise service reads them to pick one
-- contributor per task. Keyed by (plan_id, task_id, account_id) so a contributor
-- can express interest at most once per task.
CREATE TABLE IF NOT EXISTS task_interests (
    id                BIGSERIAL PRIMARY KEY,
    plan_id           TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    sow_id            TEXT,
    account_id        BIGINT NOT NULL,
    contributor_name  TEXT,
    contributor_email TEXT,
    status            TEXT NOT NULL DEFAULT 'interested',  -- interested|withdrawn|selected|rejected
    data              JSONB NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_task_interests_plan_task_acct
    ON task_interests(plan_id, task_id, account_id);
CREATE INDEX IF NOT EXISTS idx_task_interests_plan_task ON task_interests(plan_id, task_id);
CREATE INDEX IF NOT EXISTS idx_task_interests_acct ON task_interests(account_id);

-- ── Mentorship opt-in (contributor self-service) ─────────────────────────────
-- One row per contributor; upserted on opt-in; mentor_id assigned by Glimmora.
CREATE TABLE IF NOT EXISTS contributor_mentorship (
    id            BIGSERIAL PRIMARY KEY,
    account_id    BIGINT NOT NULL UNIQUE,
    opted_in_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    focus         TEXT,
    mentor_id     TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending|assigned|active|completed|cancelled
    data          JSONB NOT NULL DEFAULT '{}',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contrib_mentorship_acct ON contributor_mentorship(account_id);

-- ── contributor_notifications extended columns (FE NotificationSummary shape) ─
-- These are idempotent additions on top of the existing table that already has
-- id / account_id / title / body / category / is_read / data / created_at.
ALTER TABLE contributor_notifications ADD COLUMN IF NOT EXISTS kind         TEXT DEFAULT 'system.generic';
ALTER TABLE contributor_notifications ADD COLUMN IF NOT EXISTS severity     TEXT DEFAULT 'informational';
ALTER TABLE contributor_notifications ADD COLUMN IF NOT EXISTS action_url   TEXT;
ALTER TABLE contributor_notifications ADD COLUMN IF NOT EXISTS action_label TEXT;
ALTER TABLE contributor_notifications ADD COLUMN IF NOT EXISTS resource_type TEXT;
ALTER TABLE contributor_notifications ADD COLUMN IF NOT EXISTS resource_id   TEXT;
ALTER TABLE contributor_notifications ADD COLUMN IF NOT EXISTS channels      TEXT[] DEFAULT ARRAY['in_app'];
ALTER TABLE contributor_notifications ADD COLUMN IF NOT EXISTS read_at       TIMESTAMPTZ;
"""


def init_contributor_schema() -> None:
    c = conn()
    with c.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    c.commit()
    logger.info("contributor-service schema ensured.")
    # Payouts module schema (payouts + payout_methods tables).
    try:
        from contributor_app.routers.payouts import init_payouts_schema
        init_payouts_schema()
    except Exception as exc:  # noqa: BLE001
        logger.warning("payouts schema init failed: %s", exc)
    # Submissions v1 schema (submissions + submission_artifacts tables).
    try:
        from contributor_app.routers.submissions import ensure_submissions_schema
        ensure_submissions_schema()
    except Exception as exc:  # noqa: BLE001
        logger.warning("submissions schema init failed: %s", exc)


# ── generic helpers ───────────────────────────────────────────────────────────

def row_to_dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalise a RealDict row: datetimes -> iso, Decimal -> float, merge `data`."""
    if row is None:
        return None
    out: dict[str, Any] = {}
    nested = row.get("data") if isinstance(row.get("data"), dict) else None
    for k, v in row.items():
        if k == "data":
            continue
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif hasattr(v, "__float__") and not isinstance(v, (int, float, bool)):
            try:
                out[k] = float(v)
            except Exception:
                out[k] = v
        else:
            out[k] = v
    if nested:
        # nested payload keys win for echo-back, but never clobber the id/status columns
        for k, v in nested.items():
            out.setdefault(k, v)
    return out


def fetch_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    c = conn()
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [row_to_dict(r) for r in cur.fetchall()]


def fetch_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    c = conn()
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return row_to_dict(cur.fetchone())


def execute(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    c = conn()
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        row = cur.fetchone() if cur.description else None
    c.commit()
    return row_to_dict(row)


def paginate(items: list[Any], page: int, page_size: int) -> dict[str, Any]:
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    total = len(items)
    start = (page - 1) * page_size
    return {
        "items": items[start:start + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": (total + page_size - 1) // page_size if page_size else 1,
    }


# ── login_accounts lookup (read-only; created by auth-service) ────────────────

def get_account(account_id: str | int) -> dict[str, Any] | None:
    c = conn()
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM login_accounts WHERE id = %s", (account_id,))
        return cur.fetchone()


def get_account_by_email(email: str) -> dict[str, Any] | None:
    c = conn()
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM login_accounts WHERE LOWER(email) = LOWER(%s)", (email,))
        return cur.fetchone()


def create_oauth_account(*, email: str, first_name: str, last_name: str, provider: str) -> dict[str, Any]:
    """Find-or-create a contributor login account for an OAuth identity."""
    existing = get_account_by_email(email)
    if existing:
        return existing
    name = f"{first_name} {last_name}".strip() or email.split("@")[0]
    c = conn()
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO login_accounts
                (email, password_hash, provider, first_name, last_name, name, role,
                 email_verified, is_password_set)
            VALUES (%s, NULL, %s, %s, %s, %s, 'contributor', TRUE, FALSE)
            RETURNING *
            """,
            (email.lower(), provider, first_name, last_name, name),
        )
        row = cur.fetchone()
    c.commit()
    return row


# ── opportunity discovery (read shared enterprise_plans, read-only) ───────────

# A plan task is "open" (visible on the opportunity board) when it has no
# assignee yet and its status is one of these. Anything assigned/in-review/done
# is hidden.
_OPEN_TASK_STATUSES = {"todo", "open", "available", "unassigned", "backlog", "ready", "new"}


def fetch_open_plan_tasks() -> list[dict[str, Any]]:
    """Read every enterprise plan's decomposed tasks and return the OPEN ones
    (no assignee, open-ish status) flattened with plan/sow context. Read-only
    against the shared `enterprise_plans` table — the freelancer service never
    writes there. Returns [] if the table doesn't exist yet (enterprise not run).
    """
    c = conn()
    rows: list[dict[str, Any]] = []
    try:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, owner_id, owner_email, data, updated_at "
                "FROM enterprise_plans ORDER BY updated_at DESC"
            )
            plans = cur.fetchall()
    except Exception:  # noqa: BLE001 — table may not exist; surface an empty board
        ensure_pg_clean()
        return []

    for plan in plans:
        data = plan.get("data") or {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:  # noqa: BLE001
                data = {}
        sow_id = data.get("sowId") or data.get("sow_id")
        project_name = data.get("projectTitle") or data.get("title") or data.get("name") or "Project"
        tasks = data.get("tasks") or []
        if not isinstance(tasks, list):
            continue
        for t in tasks:
            if not isinstance(t, dict):
                continue
            status = str(t.get("status") or "todo").lower()
            assignee = t.get("assigneeId") or t.get("assignee_id") or t.get("contributorTaskId")
            if assignee:
                continue
            if status not in _OPEN_TASK_STATUSES:
                continue
            detail = t.get("detail") if isinstance(t.get("detail"), dict) else {}
            rows.append({
                "plan_id": plan["id"],
                "task_id": str(t.get("id") or ""),
                "sow_id": sow_id,
                "project_name": project_name,
                "milestone": t.get("milestone") or t.get("milestoneName") or detail.get("milestone") or "",
                "title": t.get("title") or "Untitled task",
                "description": detail.get("description") or t.get("description") or "",
                "technologies": (t.get("skills") or t.get("technologies")
                                 or detail.get("skills") or detail.get("technologies") or []),
                "effort_hours": (t.get("estimatedHours") or t.get("effortHours")
                                 or detail.get("estimatedHours") or detail.get("hours") or 0),
                "priority": t.get("priority") or "medium",
                "deadline": t.get("dueDate") or t.get("deadline") or detail.get("dueDate"),
                "owner_email": plan.get("owner_email"),
            })
    return rows


def fetch_plan_task(plan_id: str, task_id: str) -> dict[str, Any] | None:
    """Return a single open/any plan task (flattened) by plan+task id, or None."""
    for row in fetch_open_plan_tasks():
        if row["plan_id"] == plan_id and row["task_id"] == task_id:
            return row
    # Even if no longer "open" (e.g. already selected), allow interest lookups.
    c = conn()
    try:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, data FROM enterprise_plans WHERE id=%s", (plan_id,))
            plan = cur.fetchone()
    except Exception:  # noqa: BLE001
        ensure_pg_clean()
        return None
    if not plan:
        return None
    data = plan.get("data") or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:  # noqa: BLE001
            data = {}
    for t in (data.get("tasks") or []):
        if isinstance(t, dict) and str(t.get("id")) == str(task_id):
            detail = t.get("detail") if isinstance(t.get("detail"), dict) else {}
            return {
                "plan_id": plan_id, "task_id": task_id,
                "sow_id": data.get("sowId") or data.get("sow_id"),
                "project_name": data.get("projectTitle") or data.get("title") or "Project",
                "title": t.get("title") or "Untitled task",
                "description": detail.get("description") or "",
                "effort_hours": (t.get("estimatedHours") or t.get("effortHours") or 0),
            }
    return None


# ── seeding (idempotent, keyed by account_id) ─────────────────────────────────

def _count(table: str, account_id: int) -> int:
    c = conn()
    with c.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE account_id = %s", (account_id,))
        return int(cur.fetchone()[0])


def seed_demo_data(account_id: int) -> None:
    """Create a couple of demo rows the first time a contributor reads anything.

    DISABLED by default: this auto-seeded fake tasks/earnings/payouts/credentials
    on every contributor read, so users saw demo data instead of only what really
    flowed through the process. Set CONTRIBUTOR_DEMO_SEED=1 to re-enable for a
    fresh demo workspace."""
    import os
    if os.environ.get("CONTRIBUTOR_DEMO_SEED") != "1":
        return
    try:
        _seed(account_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("contributor seed failed for %s: %s", account_id, exc)
        ensure_pg_clean()


def _seed(account_id: int) -> None:
    c = conn()
    with c.cursor(cursor_factory=RealDictCursor) as cur:
        if _count("contributor_settings", account_id) == 0:
            cur.execute(
                "INSERT INTO contributor_settings (account_id, data) VALUES (%s, %s) "
                "ON CONFLICT (account_id) DO NOTHING",
                (account_id, Json({
                    "account": {"display_name": "", "headline": ""},
                    "notifications": {"email": True, "push": True, "task_alerts": True},
                    "locale": {"language": "en", "timezone": "UTC", "currency": "USD"},
                })),
            )

        if _count("contributor_tasks", account_id) == 0:
            for t in _demo_tasks():
                cur.execute(
                    """INSERT INTO contributor_tasks
                       (account_id, title, status, priority, category, reward, currency, data)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (account_id, t["title"], t["status"], t["priority"], t["category"],
                     t["reward"], "USD", Json(t["data"])),
                )

        if _count("contributor_notifications", account_id) == 0:
            for n in _demo_notifications():
                cur.execute(
                    "INSERT INTO contributor_notifications (account_id, title, body, category) "
                    "VALUES (%s,%s,%s,%s)",
                    (account_id, n["title"], n["body"], n["category"]),
                )

        if _count("contributor_earnings", account_id) == 0:
            cur.execute("INSERT INTO contributor_earnings (account_id, amount, status, description) "
                        "VALUES (%s, 250, 'cleared', 'Task: Translate landing page')", (account_id,))
            cur.execute("INSERT INTO contributor_earnings (account_id, amount, status, description) "
                        "VALUES (%s, 180, 'pending', 'Task: Review dataset labels')", (account_id,))

        if _count("contributor_payouts", account_id) == 0:
            cur.execute("INSERT INTO contributor_payouts (account_id, amount, status, method, reference) "
                        "VALUES (%s, 250, 'paid', 'bank_transfer', 'PAYOUT-0001')", (account_id,))

        if _count("contributor_payout_preferences", account_id) == 0:
            cur.execute(
                "INSERT INTO contributor_payout_preferences (account_id, data) VALUES (%s,%s) "
                "ON CONFLICT (account_id) DO NOTHING",
                (account_id, Json({"method": "bank_transfer", "currency": "USD", "details": {}})),
            )

        if _count("contributor_kyc", account_id) == 0:
            cur.execute(
                "INSERT INTO contributor_kyc (account_id, status, data) VALUES (%s,'not_started',%s) "
                "ON CONFLICT (account_id) DO NOTHING",
                (account_id, Json({})),
            )

        if _count("contributor_message_threads", account_id) == 0:
            cur.execute(
                "INSERT INTO contributor_message_threads (account_id, subject, counterpart, data) "
                "VALUES (%s,%s,%s,%s) RETURNING id",
                (account_id, "Welcome to Glimmora", "Glimmora Team", Json({})),
            )
            thread_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO contributor_thread_messages (thread_id, account_id, author, body) "
                "VALUES (%s,%s,'system','Welcome aboard! Reach out any time.')",
                (thread_id, account_id),
            )

        if _count("contributor_learning", account_id) == 0:
            cur.execute("INSERT INTO contributor_learning (account_id, title, provider, status, data) "
                        "VALUES (%s,'Prompt Engineering Basics','Glimmora Academy','recommended',%s)",
                        (account_id, Json({"duration_minutes": 45})))
            cur.execute("INSERT INTO contributor_learning (account_id, title, provider, status, data) "
                        "VALUES (%s,'Data Annotation Quality','Glimmora Academy','recommended',%s)",
                        (account_id, Json({"duration_minutes": 30})))

        if _count("contributor_credentials", account_id) == 0:
            cur.execute(
                "INSERT INTO contributor_credentials (account_id, title, issuer, kind, status, verification_code, data) "
                "VALUES (%s,'Verified Contributor','Glimmora','badge','issued',%s,%s)",
                (account_id, f"GC-{account_id}-0001", Json({"skills": ["communication"]})),
            )

        if _count("contributor_evidence", account_id) == 0:
            cur.execute("INSERT INTO contributor_evidence (account_id, title, kind, url, data) "
                        "VALUES (%s,'Portfolio site','link','https://example.com',%s)",
                        (account_id, Json({})))
    c.commit()


def _demo_tasks() -> list[dict[str, Any]]:
    return [
        {
            "title": "Translate marketing landing page (EN→ES)",
            "status": "available", "priority": "high", "category": "translation", "reward": 120,
            "data": {
                "description": "Translate 8 sections of the landing page into LATAM Spanish.",
                "checklist": [
                    {"id": "c1", "label": "Hero section", "done": False},
                    {"id": "c2", "label": "Features section", "done": False},
                ],
                "workroom": {"links": [], "templates": [{"id": "t1", "name": "Glossary"}]},
                "timeline": [{"event": "created", "at": _now().isoformat()}],
            },
        },
        {
            "title": "Label 200 product images",
            "status": "in_progress", "priority": "medium", "category": "annotation", "reward": 90,
            "data": {
                "description": "Bounding-box labels for catalog images.",
                "checklist": [{"id": "c1", "label": "First 100", "done": True},
                              {"id": "c2", "label": "Next 100", "done": False}],
                "workroom": {"links": [], "templates": []},
                "timeline": [{"event": "started", "at": _now().isoformat()}],
            },
        },
    ]


def _demo_notifications() -> list[dict[str, Any]]:
    return [
        {"title": "New task available", "body": "A translation task matches your skills.",
         "category": "tasks"},
        {"title": "Payout processed", "body": "Your payout of $250 was sent.", "category": "earnings"},
    ]
