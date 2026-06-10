"""Data-access helpers for login_accounts + profiles (psycopg2 RealDict)."""

from __future__ import annotations

import re
import secrets
from typing import Any

from psycopg2.extras import Json, RealDictCursor

from shared.db import ensure_pg_clean, get_pg_connection


def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def generate_company_code(org_name: str) -> str:
    """Human-readable, unique company code: <SLUG>-<3 random base32 chars>,
    e.g. 'Acme Corp' → 'ACME-7K2'. Slug = first alphanumeric token, up to 6
    chars, uppercased. Retries on the rare collision (company_code is UNIQUE)."""
    token = re.sub(r"[^A-Za-z0-9]", "", (org_name or "ORG").split(" ")[0]) or "ORG"
    slug = token[:6].upper()
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous 0/O/1/I
    conn = _conn()
    for _ in range(12):
        suffix = "".join(secrets.choice(alphabet) for _ in range(3))
        code = f"{slug}-{suffix}"
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM enterprise_profiles WHERE company_code = %s", (code,))
            if cur.fetchone() is None:
                return code
    # Extremely unlikely fallback — widen the suffix.
    return f"{slug}-{secrets.token_hex(3).upper()}"


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


# ── Tenants ──────────────────────────────────────────────────────────────────
# Tenants live in the Prisma "Tenant" table (PascalCase, quoted) — the SAME
# table the canonical frontend/backend (newfrontend) reads. NOT the snake_case
# `tenants` table. Columns: id, slug, name, domain, subscriptionTier, status,
# region, currency, timezone, contractRef, provisionedAt, usageCounters(jsonb),
# rateCards, retentionRules, ssoConfig, workforcePolicy, createdAt, updatedAt.

# UI tier (TitleCase) ↔ DB subscriptionTier (lowercase).
_TIER_TO_DB = {"enterprise": "enterprise", "growth": "growth", "pilot": "pilot", "trial": "trial"}
_TIER_TO_UI = {"enterprise": "Enterprise", "growth": "Growth", "pilot": "Pilot", "trial": "Trial"}


def tier_to_db(tier: str | None) -> str:
    return _TIER_TO_DB.get((tier or "pilot").strip().lower(), "pilot")


def tier_to_ui(tier: str | None) -> str:
    return _TIER_TO_UI.get((tier or "pilot").strip().lower(), "Pilot")


def get_tenant(tenant_id: str) -> dict[str, Any] | None:
    """Look up a tenant by id OR slug in the Prisma "Tenant" table."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT * FROM "Tenant" WHERE (id = %s OR slug = %s) AND "deletedAt" IS NULL',
                    (tenant_id, tenant_id))
        return cur.fetchone()


def create_tenant(
    *,
    tenant_id: str | None = None,
    slug: str,
    name: str,
    domain: str | None = None,
    tier: str = "enterprise",          # accepts UI TitleCase or db lowercase
    status: str = "active",
    region: str = "IN",
    currency: str = "INR",
    timezone: str = "Asia/Kolkata",
    contract_ref: str | None = None,
    usage_counters: dict[str, Any] | None = None,
    rate_cards: dict[str, Any] | None = None,
    retention_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a row into the Prisma "Tenant" table (upsert on id). Maps the
    New-Tenant wizard fields to the real columns. Returns the row."""
    tid = tenant_id or f"tnt-{slug}"
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            '''
            INSERT INTO "Tenant"
                (id, slug, name, domain, "subscriptionTier", status, region, currency,
                 timezone, "contractRef", "usageCounters", "rateCards", "retentionRules",
                 "provisionedAt", "createdAt", "updatedAt")
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), now(), now())
            ON CONFLICT (id) DO UPDATE
               SET name = EXCLUDED.name,
                   domain = EXCLUDED.domain,
                   "subscriptionTier" = EXCLUDED."subscriptionTier",
                   status = EXCLUDED.status,
                   region = EXCLUDED.region,
                   currency = EXCLUDED.currency,
                   timezone = EXCLUDED.timezone,
                   "contractRef" = EXCLUDED."contractRef",
                   "updatedAt" = now()
            RETURNING *
            ''',
            (tid, slug, name, domain, tier_to_db(tier), status, region, currency,
             timezone, contract_ref,
             Json(usage_counters) if usage_counters is not None else None,
             Json(rate_cards) if rate_cards is not None else None,
             Json(retention_rules) if retention_rules is not None else None),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def ensure_tenant(tenant_id: str, name: str, kind: str = "enterprise",
                  metadata: dict[str, Any] | None = None) -> None:
    """Create the "Tenant" row if it doesn't exist (keeps the table in sync when
    an enterprise account is provisioned with a tenant_id). Best-effort.
    tenant_id here is treated as the slug; the row id becomes tnt-<slug>."""
    try:
        if not get_tenant(tenant_id):
            slug = tenant_id[4:] if tenant_id.startswith("tnt-") else tenant_id
            create_tenant(tenant_id=tenant_id if tenant_id.startswith("tnt-") else None,
                          slug=slug, name=name, tier=kind, status="active",
                          contract_ref=(metadata or {}).get("companyCode"))
    except Exception:  # noqa: BLE001
        pass


def create_account(
    *,
    email: str,
    password_hash: str | None,
    first_name: str,
    last_name: str,
    role: str,
    phone: str | None = None,
    provider: str = "password",
    tenant_id: str | None = None,
    email_verified: bool = False,
    must_change_password: bool = False,
    approval_status: str = "approved",
) -> dict[str, Any]:
    conn = _conn()
    name = f"{first_name} {last_name}".strip()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO login_accounts
                (email, password_hash, provider, first_name, last_name, name, role,
                 phone, tenant_id, email_verified, is_password_set, must_change_password,
                 approval_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
            """,
            (email.lower(), password_hash, provider, first_name, last_name, name, role,
             phone, tenant_id, email_verified, password_hash is not None, must_change_password,
             approval_status),
        )
        row = cur.fetchone()
    conn.commit()
    return row


def set_password(account_id: str, password_hash: str, *, clear_must_change: bool = True) -> None:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE login_accounts
               SET password_hash = %s, is_password_set = TRUE,
                   must_change_password = CASE WHEN %s THEN FALSE ELSE must_change_password END,
                   updated_at = now()
             WHERE id = %s
            """,
            (password_hash, clear_must_change, account_id),
        )
    conn.commit()


def set_temp_password(account_id: str, password_hash: str) -> None:
    """Set a (re)generated default/temp password and FORCE must_change_password
    so the user is required to reset it on next login. Used by resend-credentials."""
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


def mark_login(account_id: str) -> None:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE login_accounts SET last_login_at = now() WHERE id = %s", (account_id,))
    conn.commit()


def set_email_verified(email: str) -> None:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE login_accounts SET email_verified = TRUE, updated_at = now() "
            "WHERE LOWER(email) = LOWER(%s)", (email,))
    conn.commit()


def create_pending_kyc(account_id: str, data: dict[str, Any]) -> None:
    """Create/refresh a pending contributor_kyc row so a women/student applicant
    appears in the Super Admin KYC Reviews queue. Shared DB; idempotent."""
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO contributor_kyc (account_id, status, data)
            VALUES (%s, 'pending', %s)
            ON CONFLICT (account_id) DO UPDATE
              SET status = 'pending', data = EXCLUDED.data, updated_at = now()
            """,
            (account_id, Json(data)),
        )
    conn.commit()


def set_mfa_secret(account_id: str, secret: str) -> None:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE login_accounts SET mfa_secret = %s WHERE id = %s", (secret, account_id))
    conn.commit()


def enable_mfa(account_id: str, recovery_codes: list[str]) -> None:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE login_accounts SET mfa_enabled = TRUE, mfa_recovery_codes = %s WHERE id = %s",
            (recovery_codes, account_id),
        )
    conn.commit()


def create_contributor_profile(account_id: str, body: dict[str, Any]) -> None:
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO contributor_profiles
                (account_id, contrib_type, gender, country, timezone, department_category,
                 primary_skills, secondary_skills, other_skills, availability,
                 degree, branch, linkedin, career_stage, years_experience,
                 work_start, work_end, segment, nda_signature, nda_accepted,
                 accept_tos, accept_coc, accept_privacy, marketing_opt_in, application_data)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (account_id) DO NOTHING
            """,
            (
                account_id, body.get("contributorType"), body.get("gender"), body.get("countryOfResidence"),
                body.get("timeZone"), body.get("departmentCategory"),
                body.get("primarySkills") or [], body.get("secondarySkills") or [],
                body.get("otherSkills") or [], body.get("weeklyAvailabilityHours"),
                body.get("degree"), body.get("branch"), body.get("linkedin"),
                body.get("careerStage"), body.get("yearsExperience"),
                body.get("workStart"), body.get("workEnd"), body.get("segment") or "general",
                body.get("ndaSignatoryLegalName") or "", bool(body.get("ndaSignatoryLegalName")),
                body.get("acceptTermsOfUse"), body.get("acceptCodeOfConduct"),
                body.get("acceptPrivacyPolicy"), body.get("marketingOptIn"),
                Json({
                    "org": body.get("applicationOrg"),
                    "background": body.get("applicationBackground"),
                    "docUrl": body.get("applicationDocUrl"),
                    "docs": body.get("applicationDocs") or [],
                }),
            ),
        )
    conn.commit()


def create_enterprise_profile(account_id: str, body: dict[str, Any], tenant_id: str,
                              company_code: str | None = None) -> str:
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO enterprise_profiles
                (account_id, org_name, org_type, industry, company_size, website,
                 hq_country, hq_city, admin_title, admin_dept, tenant_id, company_code,
                 accept_tos, accept_pp, accept_esa, accept_ahp)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                account_id, body.get("orgName"), body.get("orgType"), body.get("industry"),
                body.get("companySize"), body.get("website"), body.get("hqCountry"),
                body.get("hqCity"), body.get("adminTitle"), body.get("adminDept"), tenant_id, company_code,
                body.get("acceptTos"), body.get("acceptPp"), body.get("acceptEsa"), body.get("acceptAhp"),
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row["id"])
