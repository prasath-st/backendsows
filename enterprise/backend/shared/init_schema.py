"""
Postgres schema bootstrap. Idempotent DDL run on startup (or via
`python -m shared.init_schema`). Creates the shared auth tables plus the
core domain tables every service reads/writes. Mongo holds audit + flexible
documents; Redis holds OTP/sessions — neither needs DDL here.
"""

from __future__ import annotations

import logging

from shared.db import get_pg_connection

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
-- ── Shared auth: one row per login identity across all portals ──────────────
CREATE TABLE IF NOT EXISTS login_accounts (
    id                   BIGSERIAL PRIMARY KEY,
    email                TEXT NOT NULL UNIQUE,
    password_hash        TEXT,
    provider             TEXT,                       -- 'password' | 'google' | 'microsoft'
    first_name           TEXT NOT NULL DEFAULT '',
    last_name            TEXT NOT NULL DEFAULT '',
    name                 TEXT NOT NULL DEFAULT '',
    role                 TEXT NOT NULL DEFAULT 'contributor',
    phone                TEXT,
    tenant_id            TEXT,                       -- org/university/team scope
    department           TEXT,
    email_verified       BOOLEAN NOT NULL DEFAULT FALSE,
    phone_verified       BOOLEAN NOT NULL DEFAULT FALSE,
    mfa_enabled          BOOLEAN NOT NULL DEFAULT FALSE,
    mfa_secret           TEXT,
    mfa_recovery_codes   TEXT[],
    is_password_set      BOOLEAN NOT NULL DEFAULT TRUE,
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at        TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_login_accounts_role ON login_accounts(role);
CREATE INDEX IF NOT EXISTS idx_login_accounts_tenant ON login_accounts(tenant_id);
-- Approval gate: 'approved' (default — most roles need no approval) | 'pending'
-- | 'rejected'. Women freelancers self-apply and stay 'pending' until a Super
-- Admin approves; login is blocked while pending/rejected.
ALTER TABLE login_accounts ADD COLUMN IF NOT EXISTS approval_status TEXT NOT NULL DEFAULT 'approved';
-- Login resolves accounts with WHERE LOWER(email) = LOWER($1). A plain index on
-- `email` can't serve that predicate, so without this functional index every
-- login does a SEQ SCAN. This makes email lookup an index hit (microseconds),
-- which is the real login speed win (far more than splitting tables would give).
CREATE UNIQUE INDEX IF NOT EXISTS idx_login_accounts_email_lower ON login_accounts(LOWER(email));

-- ── Refresh-token / session registry ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS auth_sessions (
    id              BIGSERIAL PRIMARY KEY,
    account_id      BIGINT NOT NULL REFERENCES login_accounts(id) ON DELETE CASCADE,
    refresh_token   TEXT NOT NULL,
    user_agent      TEXT,
    ip_address      TEXT,
    device          TEXT,
    is_current      BOOLEAN NOT NULL DEFAULT TRUE,
    revoked         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_account ON auth_sessions(account_id);

-- ── Contributor profile (freelancer) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS contributor_profiles (
    id                  BIGSERIAL PRIMARY KEY,
    account_id          BIGINT NOT NULL UNIQUE REFERENCES login_accounts(id) ON DELETE CASCADE,
    contrib_type        TEXT,
    gender              TEXT,
    country             TEXT,
    city                TEXT,
    dob                 DATE,
    timezone            TEXT,
    department_category TEXT,
    department_other    TEXT,
    primary_skills      TEXT[] DEFAULT '{}',
    secondary_skills    TEXT[] DEFAULT '{}',
    other_skills        TEXT[] DEFAULT '{}',
    availability        TEXT,
    weekly_hours        NUMERIC,
    degree              TEXT,
    branch              TEXT,
    linkedin            TEXT,
    bio                 TEXT,
    job_title           TEXT,
    career_stage        TEXT,
    years_experience    TEXT,
    work_start          TEXT,
    work_end            TEXT,
    segment             TEXT DEFAULT 'general',     -- 'general' | 'women' | 'university' | 'student'
    nda_accepted        BOOLEAN DEFAULT FALSE,
    nda_signature       TEXT DEFAULT '',
    accept_tos          BOOLEAN DEFAULT FALSE,
    accept_coc          BOOLEAN DEFAULT FALSE,
    accept_privacy      BOOLEAN DEFAULT FALSE,
    marketing_opt_in    BOOLEAN DEFAULT FALSE,
    -- Women-in-Tech application payload: { org, background, docs: [{url,filename,contentType}] }
    application_data    JSONB DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Enterprise organisation profile ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS enterprise_profiles (
    id              BIGSERIAL PRIMARY KEY,
    account_id      BIGINT NOT NULL REFERENCES login_accounts(id) ON DELETE CASCADE,
    org_name        TEXT NOT NULL,
    org_type        TEXT,
    industry        TEXT,
    company_size    TEXT,
    website         TEXT,
    hq_country      TEXT,
    hq_city         TEXT,
    admin_title     TEXT,
    admin_dept      TEXT,
    tenant_id       TEXT,
    company_code    TEXT UNIQUE,
    accept_tos      BOOLEAN DEFAULT FALSE,
    accept_pp       BOOLEAN DEFAULT FALSE,
    accept_esa      BOOLEAN DEFAULT FALSE,
    accept_ahp      BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Generic tenant table (universities / women-teams / orgs) ────────────────
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,                       -- 'university' | 'women_team' | 'enterprise'
    metadata    JSONB DEFAULT '{}',
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Platform settings (singleton) + pricing config ─────────────────────────
CREATE TABLE IF NOT EXISTS platform_settings (
    id      INT PRIMARY KEY DEFAULT 1,
    data    JSONB NOT NULL DEFAULT '{}'
);
INSERT INTO platform_settings (id, data) VALUES (1, '{}'::jsonb)
    ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS contributor_pricing (
    id          INT PRIMARY KEY DEFAULT 1,
    data        JSONB NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO contributor_pricing (id, data) VALUES (1, '{}'::jsonb)
    ON CONFLICT (id) DO NOTHING;
"""


def init_schema() -> None:
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()
    logger.info("Postgres schema ensured.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_schema()
    print("Schema initialised.")
