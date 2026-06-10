"""
Tenant list / detail endpoints for the Super Admin Tenants page.

GET /api/superadmin/tenants          — paginated list matching MockTenant shape
GET /api/superadmin/tenants/{tid}    — single tenant detail (same shape)

Read-only over existing tables:
  tenants            (id, name, kind, metadata, is_active, created_at)
  login_accounts     (tenant_id, role  → user count per tenant)
  enterprise_sows    (owner_id  → sow count via owner's tenant_id)
  tenant_subscriptions (plan_code, tenant_status → tier / status override)

No audit for plain reads.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_admin
from shared.mailer import build_credentials_body, send_email
from shared.security import generate_temp_password, hash_password

from auth_app import repo as auth_repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["superadmin-tenants"])


# ── helpers ───────────────────────────────────────────────────────────────────

def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


# Plan code → FE tier label mapping (mirrors tenant_subscription PLAN_CATALOG).
_PLAN_TIER: dict[str, str] = {
    "enterprise": "Enterprise",
    "growth": "Growth",
    "pilot": "Pilot",
    "trial": "Trial",
}

_KIND_TIER: dict[str, str] = {
    "enterprise": "Enterprise",
    "women_team": "Pilot",
    "university": "Growth",
}


def _derive_status(is_active: bool, metadata: dict[str, Any], sub_status: str | None) -> str:
    """Map DB fields → MockTenant TenantStatus.

    Priority:
      1. If the tenant_subscriptions row has a non-null tenant_status, use it
         (values already match: active / provisioning / paused / draft / closed).
      2. Else fall back to metadata["status"] if present.
      3. Else derive from is_active flag.
    """
    if sub_status and sub_status in ("active", "provisioning", "paused", "draft", "closed"):
        return sub_status
    meta_status = metadata.get("status")
    if meta_status and meta_status in ("active", "provisioning", "paused", "draft", "closed"):
        return meta_status
    return "active" if is_active else "paused"


def _derive_domain(tenant_id: str, metadata: dict[str, Any]) -> str:
    """Return the domain string from metadata or construct a fallback."""
    domain = metadata.get("domain") or metadata.get("website") or ""
    if domain:
        # Strip protocol if present.
        for pfx in ("https://", "http://"):
            if domain.startswith(pfx):
                domain = domain[len(pfx):]
        return domain.rstrip("/")
    # Fallback: slug from tenant id  (e.g. "t-acme-corp" → "acme-corp.com")
    slug = tenant_id.removeprefix("t-").replace("_", "-")
    return f"{slug}.com"


def _derive_region(metadata: dict[str, Any]) -> str:
    return (
        metadata.get("region")
        or metadata.get("hq_region")
        or "Asia-South · INR"
    )


def _derive_currency(metadata: dict[str, Any]) -> str:
    return (
        metadata.get("currency")
        or metadata.get("billing_currency")
        or "INR"
    )


def _derive_msa_ref(tenant_id: str, metadata: dict[str, Any]) -> str:
    return metadata.get("msaRef") or metadata.get("msa_ref") or f"MSA-{tenant_id}"


_DB_TIER_TO_UI = {"enterprise": "Enterprise", "growth": "Growth", "pilot": "Pilot", "trial": "Trial"}
_REGION_LABEL = {"IN": "Asia-South", "US": "Americas", "EU": "Europe", "UK": "Europe"}


def _tenant_out(
    t_row: dict[str, Any],
    user_count: int,
    sow_count: int,
    sub_row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Map a Prisma "Tenant" row into the MockTenant shape the FE expects.
    (Columns: id, slug, name, domain, subscriptionTier, status, region,
    currency, contractRef, provisionedAt, createdAt, retentionRules, ...)"""
    tier = _DB_TIER_TO_UI.get((t_row.get("subscriptionTier") or "pilot").lower(), "Pilot")
    region_code = t_row.get("region") or "IN"
    currency = t_row.get("currency") or "INR"
    region_disp = f"{_REGION_LABEL.get(region_code, region_code)} · {currency}"
    counters = t_row.get("usageCounters") or {}
    if isinstance(counters, str):
        try:
            counters = json.loads(counters)
        except (ValueError, TypeError):
            counters = {}

    return {
        "id": t_row["id"],
        "slug": t_row.get("slug"),
        "name": t_row.get("name") or t_row["id"],
        "domain": t_row.get("domain") or "",
        "tier": tier,
        "status": t_row.get("status") or "active",
        "users": user_count,
        "sows": sow_count,
        "provisionedAt": _iso(t_row.get("provisionedAt") or t_row.get("createdAt")),
        "msaRef": t_row.get("contractRef") or "",
        "region": region_disp,
        "currency": currency,
        "payouts30d": counters.get("payouts30d") or None,
        "lastHrisSyncAt": _iso(t_row.get("lastHrisSyncAt")) if t_row.get("lastHrisSyncAt") else None,
    }


# ── bulk data fetchers ────────────────────────────────────────────────────────

_TENANT_COLS = ('id, slug, name, domain, "subscriptionTier", status, region, currency, '
                '"contractRef", "usageCounters", "provisionedAt", "createdAt"')


def _fetch_all_tenants(conn) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f'SELECT {_TENANT_COLS} FROM "Tenant" WHERE "deletedAt" IS NULL '
                    'ORDER BY "createdAt" DESC')
        return list(cur.fetchall())


def _fetch_user_counts(conn) -> dict[str, int]:
    """Count login_accounts grouped by tenant_id."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, COUNT(*) AS n FROM login_accounts "
            "WHERE tenant_id IS NOT NULL GROUP BY tenant_id"
        )
        return {row[0]: int(row[1]) for row in cur.fetchall()}


def _fetch_sow_counts(conn) -> dict[str, int]:
    """Count enterprise_sows grouped by tenant_id of the owner account.

    enterprise_sows.owner_id is the login_accounts.id (as TEXT) of the
    uploader. We join to login_accounts to get their tenant_id, then group.
    Falls back to 0 if enterprise_sows doesn't exist in this DB.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.tenant_id, COUNT(s.id) AS n
                  FROM enterprise_sows s
                  JOIN login_accounts a ON a.id::text = s.owner_id
                 WHERE a.tenant_id IS NOT NULL
                 GROUP BY a.tenant_id
                """
            )
            return {row[0]: int(row[1]) for row in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001 — table may be in another service's schema
        logger.debug("Could not count enterprise_sows: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return {}


def _fetch_subscriptions(conn) -> dict[str, dict[str, Any]]:
    """tenant_id → subscription row (plan_code, tenant_status)."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT tenant_id, plan_code, tenant_status FROM tenant_subscriptions")
            return {row["tenant_id"]: dict(row) for row in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not fetch tenant_subscriptions: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return {}


# ── GET /api/superadmin/tenants ───────────────────────────────────────────────

@router.get("/api/superadmin/tenants")
async def list_tenants(
    admin: Annotated[dict, Depends(get_current_admin)],
    status: str | None = None,
    tier: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    """List all enterprise tenants for the Super Admin Tenants page.

    Returns MockTenant-shaped items augmented with live user + SOW counts.
    Optional query params: status, tier, q (name/domain search), page, page_size.
    """
    conn = _conn()

    tenants = _fetch_all_tenants(conn)
    user_counts = _fetch_user_counts(conn)
    sow_counts = _fetch_sow_counts(conn)
    subscriptions = _fetch_subscriptions(conn)

    items: list[dict[str, Any]] = []
    for t in tenants:
        tid = t["id"]
        out = _tenant_out(
            t,
            user_count=user_counts.get(tid, 0),
            sow_count=sow_counts.get(tid, 0),
            sub_row=subscriptions.get(tid),
        )
        items.append(out)

    # Optional server-side filtering (FE also filters, but useful for API callers).
    if status:
        items = [i for i in items if i["status"] == status]
    if tier:
        items = [i for i in items if i["tier"].lower() == tier.lower()]
    if q:
        needle = q.strip().lower()
        items = [
            i for i in items
            if needle in (i.get("name") or "").lower()
            or needle in (i.get("domain") or "").lower()
            or needle in (i.get("msaRef") or "").lower()
            or needle in i["id"].lower()
        ]

    total = len(items)
    page_size = max(1, min(page_size, 200))
    page = max(1, page)
    offset = (page - 1) * page_size
    page_items = items[offset: offset + page_size]

    return {
        "items": page_items,
        "total": total,
        "page": page,
        "pageSize": page_size,
    }


# ── GET /api/superadmin/tenants/{tenant_id} ───────────────────────────────────

@router.get("/api/superadmin/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Return a single tenant detail in MockTenant shape."""
    conn = _conn()

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f'SELECT {_TENANT_COLS} FROM "Tenant" WHERE (id = %s OR slug = %s) AND "deletedAt" IS NULL',
            (tenant_id, tenant_id),
        )
        t_row = cur.fetchone()

    if not t_row:
        raise HTTPException(status_code=404, detail={"error": "tenant_not_found", "tenantId": tenant_id})

    user_counts = _fetch_user_counts(conn)
    sow_counts = _fetch_sow_counts(conn)
    subscriptions = _fetch_subscriptions(conn)

    return _tenant_out(
        t_row,
        user_count=user_counts.get(tenant_id, 0),
        sow_count=sow_counts.get(tenant_id, 0),
        sub_row=subscriptions.get(tenant_id),
    )


# ── POST /api/superadmin/tenants  (create a tenant + optional first admin) ─────

class _CreateTenantRequest(BaseModel):
    """Accepts the full New-Tenant wizard payload (6 steps). Only name + a
    tenant id (slug or auto) are strictly required; the rest are stored as
    tenant metadata or used to provision the first admin."""
    name: str
    # Step 1 — tenant info
    slug: str | None = None           # user-chosen Tenant ID (URL slug)
    tenantId: str | None = None       # alias for slug
    domain: str | None = None
    tier: str | None = None           # Enterprise | Growth | Pilot
    msaRef: str | None = None
    kind: str = "enterprise"          # enterprise | university | women_team
    # Step 2 — primary admin
    adminEmail: str | None = None
    adminName: str | None = None      # single name field from the wizard
    adminFirstName: str | None = None
    adminLastName: str | None = None
    # Step 3 — licensed roles
    rolesEnabled: dict[str, bool] | None = None
    # Step 4 — region & currency
    region: str | None = None
    currency: str | None = None
    timezone: str | None = None
    # Step 5 — compliance baseline
    retentionAudit: str | None = None
    retentionEvidence: str | None = None
    residencyRegion: str | None = None
    consentVersion: str | None = None


def _slugify(name: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "-", (name or "tenant").lower()).strip("-")[:40] or "tenant"


def _slug_tenant_id(name: str, kind: str) -> str:
    import secrets as _secrets
    prefix = {"university": "uni", "women_team": "wt"}.get(kind, "ent")
    return f"{prefix}-{_slugify(name)[:24]}-{_secrets.token_hex(3)}"


@router.post("/api/superadmin/tenants", status_code=201)
async def create_tenant(
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _CreateTenantRequest = Body(...),
):
    """Create a new tenant (super-admin provisioning) from the New-Tenant wizard.
    Uses the wizard's slug as the tenant id when provided; stores tier / MSA ref /
    licensed roles / region+currency / compliance baseline as metadata; and
    optionally provisions the first admin with a default password (emailed +
    forced reset on first login)."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    kind = body.kind if body.kind in ("enterprise", "university", "women_team") else "enterprise"

    # Prefer the wizard's user-chosen slug/tenantId; fall back to a slug from name.
    slug = _slugify((body.slug or body.tenantId or name).strip())
    tenant_id = f"tnt-{slug}"
    if auth_repo.get_tenant(slug) or auth_repo.get_tenant(tenant_id):
        raise HTTPException(status_code=409, detail=f"Tenant '{slug}' already exists")

    # Compliance baseline + licensed roles → retentionRules / usageCounters JSONB.
    retention_rules = {
        "retentionAudit": body.retentionAudit,
        "retentionEvidence": body.retentionEvidence,
        "residencyRegion": body.residencyRegion,
        "consentVersion": body.consentVersion,
    }
    usage_counters = {"rolesEnabled": body.rolesEnabled or {}, "createdBy": admin.get("email")}

    tenant = auth_repo.create_tenant(
        tenant_id=tenant_id, slug=slug, name=name,
        domain=body.domain or None,
        tier=body.tier or kind,            # UI TitleCase or kind → mapped to lowercase in repo
        status="active",
        region=body.region or "IN",
        currency=body.currency or "INR",
        timezone=body.timezone or "Asia/Kolkata",
        contract_ref=body.msaRef,
        usage_counters=usage_counters,
        retention_rules=retention_rules,
    )

    provisioned_admin = None
    if body.adminEmail:
        email = body.adminEmail.strip().lower()
        if auth_repo.find_account_by_email(email):
            raise HTTPException(status_code=409, detail="An account with that admin email already exists")
        # Split the wizard's single adminName into first/last (fallback to explicit fields).
        first = body.adminFirstName
        last = body.adminLastName
        if not first and body.adminName:
            parts = body.adminName.strip().split(" ", 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else ""
        temp = generate_temp_password()
        acct = auth_repo.create_account(
            email=email, password_hash=hash_password(temp),
            first_name=first or "", last_name=last or "",
            role="enterprise", provider="password", email_verified=True,
            tenant_id=tenant_id, must_change_password=True,
        )
        try:
            login_url = getattr(__import__("shared.config", fromlist=["settings"]).settings,
                                "app_base_url", None) or "https://app.glimmora.ai"
            text, html = build_credentials_body(
                name=(first or email), email=email, temp_password=temp,
                login_url=login_url, org_name=name)
            send_email(to_email=email, subject="Your Glimmora admin credentials",
                       body=text, html=html)
        except Exception:  # noqa: BLE001
            pass
        provisioned_admin = {"id": str(acct.get("id")) if isinstance(acct, dict) else None,
                             "email": email, "mustChangePassword": True}

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="create_tenant",
            target=name, target_id=tenant_id, tenant_id=tenant_id,
        )
    except Exception:  # noqa: BLE001
        pass

    # Return the MockTenant shape the frontend consumes (TitleCase tier, msaRef,
    # region·currency, users/sows counts).
    region_disp = f"{body.region or 'IN'} · {body.currency or 'INR'}"
    return {
        "id": tenant_id, "slug": slug, "name": name,
        "domain": body.domain or "", "tier": auth_repo.tier_to_ui(body.tier or kind),
        "status": "active", "users": 1 if provisioned_admin else 0, "sows": 0,
        "provisionedAt": _iso(tenant.get("provisionedAt") or tenant.get("createdAt"))
            if isinstance(tenant, dict) else None,
        "msaRef": body.msaRef or "", "region": region_disp,
        "currency": body.currency or "INR",
        "admin": provisioned_admin,
    }
