"""Superadmin-service specific DDL (reviewer assignments + recommendations).

Reuses login_accounts + contributor_pricing from shared.init_schema; only adds
the reviewer-workflow tables this service owns.
"""

from __future__ import annotations

import logging

from shared.db import get_pg_connection

logger = logging.getLogger(__name__)

SUPERADMIN_SCHEMA_SQL = """
-- ── Contributor KYC submissions (admin KYC review queue + detail) ───────────
CREATE TABLE IF NOT EXISTS contributor_kyc (
    account_id   BIGINT PRIMARY KEY REFERENCES login_accounts(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    segment      TEXT,
    data         JSONB NOT NULL DEFAULT '{}',
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_contributor_kyc_status ON contributor_kyc(status);

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

-- ── Tenant SSO configurations (SAML 2.0 + OIDC) ─────────────────────────────
-- One row per tenant; sso_config is the TenantSsoConfig JSON shape the FE
-- library defines. slug is the unique tenant identifier used in callback URLs.
CREATE TABLE IF NOT EXISTS tenant_sso_configs (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL UNIQUE,          -- FK-like: matches tenants.id or enterprise_profiles.tenant_id
    slug        TEXT NOT NULL UNIQUE,          -- URL-safe tenant slug, e.g. "acme-corp"
    tenant_name TEXT,
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    kind        TEXT NOT NULL DEFAULT 'saml',  -- 'saml' | 'oidc'
    -- Default role assigned to JIT-provisioned users from this tenant (enterprise|reviewer)
    default_role TEXT NOT NULL DEFAULT 'enterprise',
    sso_config  JSONB NOT NULL DEFAULT '{}',   -- full TenantSsoConfig JSON
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tenant_sso_configs_slug ON tenant_sso_configs(slug);
CREATE INDEX IF NOT EXISTS idx_tenant_sso_configs_tenant ON tenant_sso_configs(tenant_id);

-- ── SSO durable sessions ─────────────────────────────────────────────────────
-- Created when a JIT-provisioned user signs in via SSO (SAML/OIDC callback).
-- Gives the FE a sessionId to carry alongside the access token.
CREATE TABLE IF NOT EXISTS sso_sessions (
    id          TEXT PRIMARY KEY,              -- UUID session id returned to FE
    account_id  BIGINT REFERENCES login_accounts(id) ON DELETE CASCADE,
    tenant_id   TEXT,
    kind        TEXT,                          -- 'saml' | 'oidc'
    ip_address  TEXT,
    user_agent  TEXT,
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sso_sessions_account ON sso_sessions(account_id);

-- ── Reviewer invite codes ─────────────────────────────────────────────────────
-- Admin creates an invite for a specific email; reviewer self-registers with
-- the code. Persisted to Postgres so invites survive restarts (unlike the
-- in-memory FE store). Expiry: 7 days; idempotent on (email, status=pending).
CREATE TABLE IF NOT EXISTS reviewer_invites (
    id              BIGSERIAL PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    email           TEXT NOT NULL,
    tenant_id       TEXT,
    invited_by_id   TEXT,
    invited_by_name TEXT,
    invited_by_email TEXT,
    org_name        TEXT,
    note            TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | expired
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '7 days'),
    accepted_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_reviewer_invites_code  ON reviewer_invites(code);
CREATE INDEX IF NOT EXISTS idx_reviewer_invites_email ON reviewer_invites(LOWER(email));
CREATE INDEX IF NOT EXISTS idx_reviewer_invites_status ON reviewer_invites(status);

-- ── Tenant subscriptions ─────────────────────────────────────────────────────
-- One row per enterprise tenant. plan_code matches FE PlanCode type.
CREATE TABLE IF NOT EXISTS tenant_subscriptions (
    tenant_id               TEXT PRIMARY KEY,
    tenant_name             TEXT,
    tenant_slug             TEXT,
    tenant_status           TEXT NOT NULL DEFAULT 'active',
    plan_code               TEXT NOT NULL DEFAULT 'pilot',
    contract_ref            TEXT,
    usage_counters          JSONB NOT NULL DEFAULT '{}',
    subscription_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    trial_ends_at           TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tenant_subs_plan ON tenant_subscriptions(plan_code);

-- ── Tenant subscription history ──────────────────────────────────────────────
-- Immutable append log; one row per admin-triggered plan change.
CREATE TABLE IF NOT EXISTS tenant_subscription_history (
    id                  TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    tenant_id           TEXT NOT NULL,
    from_plan           TEXT,
    to_plan             TEXT NOT NULL,
    changed_by_user_id  TEXT,
    changed_by_name     TEXT,
    changed_by_email    TEXT,
    changed_by_role     TEXT,
    source              TEXT NOT NULL DEFAULT 'admin',
    contract_ref        TEXT,
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tenant_sub_hist_tenant  ON tenant_subscription_history(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tenant_sub_hist_created ON tenant_subscription_history(created_at DESC);
"""


def init_superadmin_schema() -> None:
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(SUPERADMIN_SCHEMA_SQL)
    conn.commit()
    logger.info("Superadmin-service schema ensured.")
    # Also ensure platform-ops tables (email_sent_records, submission_artifacts)
    try:
        from superadmin_app.routers.platform_ops import init_platform_ops_schema
        init_platform_ops_schema()
    except Exception as exc:  # noqa: BLE001
        logger.warning("platform-ops schema init failed: %s", exc)
    # AI-agents schema (agents, prompt_templates, prompt_versions, invocations)
    try:
        from superadmin_app.routers.ai_agents import init_ai_agents_schema
        init_ai_agents_schema()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai-agents schema init failed: %s", exc)
