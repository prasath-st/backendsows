"""
Mentor-service Postgres schema. Idempotent DDL run on startup after the shared
init_schema(). Nested/flexible payloads (rubric scores, checklists, history
trails, settings blobs) are stored in JSONB columns.
"""

from __future__ import annotations

import logging

from shared.db import get_pg_connection

logger = logging.getLogger(__name__)

MENTOR_SCHEMA_SQL = """
-- ── Coaching sessions (mentor ↔ contributor) ────────────────────────────────
CREATE TABLE IF NOT EXISTS mentor_sessions (
    id               TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    mentor_id        TEXT NOT NULL,
    contributor_id   TEXT NOT NULL,
    tenant_id        TEXT,
    scheduled_at     TIMESTAMPTZ NOT NULL,
    duration_minutes INT NOT NULL DEFAULT 30,
    agenda           TEXT,
    meeting_link     TEXT,
    timezone         TEXT,
    status           TEXT NOT NULL DEFAULT 'scheduled',
    -- scheduled | held | no_show | cancelled | rescheduled
    cancel_reason    TEXT,
    created_by       TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mentor_sessions_mentor   ON mentor_sessions(mentor_id);
CREATE INDEX IF NOT EXISTS idx_mentor_sessions_contrib  ON mentor_sessions(contributor_id);
CREATE INDEX IF NOT EXISTS idx_mentor_sessions_status   ON mentor_sessions(status);
CREATE INDEX IF NOT EXISTS idx_mentor_sessions_sched    ON mentor_sessions(scheduled_at);

-- Extend mentor_notes with new columns (idempotent ALTER)
ALTER TABLE mentor_notes ADD COLUMN IF NOT EXISTS session_id    TEXT;
ALTER TABLE mentor_notes ADD COLUMN IF NOT EXISTS contributor_id TEXT;
ALTER TABLE mentor_notes ADD COLUMN IF NOT EXISTS visibility    TEXT NOT NULL DEFAULT 'private';
ALTER TABLE mentor_notes ADD COLUMN IF NOT EXISTS deleted_at    TIMESTAMPTZ;

-- ── Review queue items assigned to a mentor ─────────────────────────────────
CREATE TABLE IF NOT EXISTS mentor_reviews (
    id              BIGSERIAL PRIMARY KEY,
    mentor_id       TEXT NOT NULL,                 -- login_accounts.id (as text)
    mentor_email    TEXT,
    tenant_id       TEXT,
    title           TEXT NOT NULL DEFAULT '',
    submission_type TEXT NOT NULL DEFAULT 'content',  -- content|sow|assignment|...
    contributor_id  TEXT,
    contributor_name TEXT,
    mentee_id       BIGINT,                        -- mentor_mentorships.id (optional link)
    priority        TEXT NOT NULL DEFAULT 'normal',   -- low|normal|high|urgent
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|in_review|accepted|rework|escalated
    decision        TEXT,                          -- accept|rework|escalate
    score           NUMERIC,
    comments        TEXT,
    payload         JSONB NOT NULL DEFAULT '{}',   -- nested submission data
    rubric          JSONB NOT NULL DEFAULT '{}',   -- nested rubric/criteria scores
    decided_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mentor_reviews_mentor ON mentor_reviews(mentor_id);
CREATE INDEX IF NOT EXISTS idx_mentor_reviews_status ON mentor_reviews(status);
-- claimed_by tracks which mentor claimed a pool item (v1 submissions endpoints)
ALTER TABLE mentor_reviews ADD COLUMN IF NOT EXISTS claimed_by TEXT;
CREATE INDEX IF NOT EXISTS idx_mentor_reviews_claimed ON mentor_reviews(claimed_by);

-- ── Mentorship relationships (mentor → mentee) ──────────────────────────────
CREATE TABLE IF NOT EXISTS mentor_mentorships (
    id              BIGSERIAL PRIMARY KEY,
    mentor_id       TEXT NOT NULL,
    tenant_id       TEXT,
    mentee_name     TEXT NOT NULL DEFAULT '',
    mentee_email    TEXT,
    mentee_account_id TEXT,
    role            TEXT,                          -- contributor|student|...
    track           TEXT,
    status          TEXT NOT NULL DEFAULT 'active',   -- active|paused|completed
    progress        NUMERIC NOT NULL DEFAULT 0,    -- 0..100
    goals           JSONB NOT NULL DEFAULT '[]',   -- nested goals list
    meta            JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mentor_mentorships_mentor ON mentor_mentorships(mentor_id);

-- ── Escalations raised by a mentor ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mentor_escalations (
    id              BIGSERIAL PRIMARY KEY,
    mentor_id       TEXT NOT NULL,
    mentor_email    TEXT,
    tenant_id       TEXT,
    review_id       BIGINT,                        -- optional link to mentor_reviews
    mentee_id       BIGINT,                        -- optional link to mentor_mentorships
    subject         TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT 'general',  -- quality|conduct|technical|general
    priority        TEXT NOT NULL DEFAULT 'normal',
    status          TEXT NOT NULL DEFAULT 'open',  -- open|in_progress|resolved|closed
    description     TEXT,
    assignee        TEXT,
    timeline        JSONB NOT NULL DEFAULT '[]',   -- nested status-change trail
    meta            JSONB NOT NULL DEFAULT '{}',
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mentor_escalations_mentor ON mentor_escalations(mentor_id);
CREATE INDEX IF NOT EXISTS idx_mentor_escalations_status ON mentor_escalations(status);

-- ── Free-form notes (mentorship notes, review notes) ────────────────────────
CREATE TABLE IF NOT EXISTS mentor_notes (
    id              BIGSERIAL PRIMARY KEY,
    mentor_id       TEXT NOT NULL,
    tenant_id       TEXT,
    mentee_id       BIGINT,                        -- mentor_mentorships.id
    review_id       BIGINT,                        -- mentor_reviews.id
    kind            TEXT NOT NULL DEFAULT 'mentorship', -- mentorship|review|general
    body            TEXT NOT NULL DEFAULT '',
    attachments     JSONB NOT NULL DEFAULT '[]',   -- nested attachment list
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_mentor_notes_mentee ON mentor_notes(mentee_id);
CREATE INDEX IF NOT EXISTS idx_mentor_notes_mentor ON mentor_notes(mentor_id);

-- ── Mentor profile + settings (one row per mentor) ──────────────────────────
CREATE TABLE IF NOT EXISTS mentor_profiles (
    mentor_id       TEXT PRIMARY KEY,
    tenant_id       TEXT,
    display_name    TEXT NOT NULL DEFAULT '',
    headline        TEXT NOT NULL DEFAULT '',
    bio             TEXT NOT NULL DEFAULT '',
    expertise       JSONB NOT NULL DEFAULT '[]',   -- nested skill tags
    languages       JSONB NOT NULL DEFAULT '[]',
    timezone        TEXT,
    avatar_url      TEXT,
    links           JSONB NOT NULL DEFAULT '{}',
    settings        JSONB NOT NULL DEFAULT '{}',   -- notification/availability prefs
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def init_mentor_schema() -> None:
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(MENTOR_SCHEMA_SQL)
    conn.commit()
    logger.info("Mentor schema ensured.")
