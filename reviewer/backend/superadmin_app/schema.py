"""Superadmin-service specific DDL (reviewer assignments + recommendations).

Reuses login_accounts + contributor_pricing from shared.init_schema; only adds
the reviewer-workflow tables this service owns.
"""

from __future__ import annotations

import logging

from shared.db import get_pg_connection

logger = logging.getLogger(__name__)

SUPERADMIN_SCHEMA_SQL = """
-- ── Reviewer assignments (a reviewer ↔ project/evidence link) ───────────────
CREATE TABLE IF NOT EXISTS reviewer_assignments (
    id              BIGSERIAL PRIMARY KEY,
    reviewer_id     BIGINT REFERENCES login_accounts(id) ON DELETE CASCADE,
    reviewer_email  TEXT,
    project_id      TEXT,
    project_name    TEXT,
    title           TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending | in_review | approved | rejected | completed
    priority        TEXT DEFAULT 'normal',
    data            JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reviewer_assignments_reviewer ON reviewer_assignments(reviewer_id);
CREATE INDEX IF NOT EXISTS idx_reviewer_assignments_status   ON reviewer_assignments(status);

-- ── Reviewer recommendations on a piece of evidence ─────────────────────────
CREATE TABLE IF NOT EXISTS reviewer_recommendations (
    id              BIGSERIAL PRIMARY KEY,
    evidence_id     TEXT NOT NULL,
    assignment_id   BIGINT REFERENCES reviewer_assignments(id) ON DELETE SET NULL,
    reviewer_id     BIGINT REFERENCES login_accounts(id) ON DELETE CASCADE,
    reviewer_email  TEXT,
    recommendation  TEXT,                              -- approve | reject | revise | ...
    score           NUMERIC,
    comment         TEXT,
    data            JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reviewer_reco_evidence ON reviewer_recommendations(evidence_id);
CREATE INDEX IF NOT EXISTS idx_reviewer_reco_reviewer ON reviewer_recommendations(reviewer_id);

-- ── Generic admin records: one JSONB-backed table serving every superadmin
-- section (tenants, mentors, skill-taxonomy, kyc, governance, payment-rails,
-- rubric-templates, partnerships, ai, system-health). Keyed by `kind` so each
-- section gets real CRUD + persistence without bespoke tables. Soft-delete via
-- deleted_at (educore pattern).
CREATE TABLE IF NOT EXISTS admin_records (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT,
    status      TEXT,
    data        JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_admin_records_kind ON admin_records(kind) WHERE deleted_at IS NULL;
"""


def init_superadmin_schema() -> None:
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(SUPERADMIN_SCHEMA_SQL)
    conn.commit()
    logger.info("Superadmin-service schema ensured.")
