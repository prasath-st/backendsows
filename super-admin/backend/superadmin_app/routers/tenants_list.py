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

from fastapi import APIRouter, Depends, HTTPException
from psycopg2.extras import RealDictCursor

from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_admin

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


def _tenant_out(
    t_row: dict[str, Any],
    user_count: int,
    sow_count: int,
    sub_row: dict[str, Any] | None,
) -> dict[str, Any]:
    """Convert raw DB rows into the MockTenant shape the FE expects."""
    metadata: dict[str, Any] = t_row.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}

    is_active: bool = bool(t_row.get("is_active", True))

    # Tier: prefer subscription plan_code, fall back to kind or metadata.
    if sub_row and sub_row.get("plan_code"):
        tier = _PLAN_TIER.get(sub_row["plan_code"], "Pilot")
    else:
        tier = (
            _PLAN_TIER.get(metadata.get("tier", "").lower(), None)
            or _KIND_TIER.get(t_row.get("kind", ""), "Pilot")
        )

    sub_status = sub_row.get("tenant_status") if sub_row else None

    return {
        "id": t_row["id"],
        "name": t_row.get("name") or t_row["id"],
        "domain": _derive_domain(t_row["id"], metadata),
        "tier": tier,
        "status": _derive_status(is_active, metadata, sub_status),
        "users": user_count,
        "sows": sow_count,
        "provisionedAt": _iso(t_row.get("created_at")),
        "msaRef": _derive_msa_ref(t_row["id"], metadata),
        "region": _derive_region(metadata),
        "currency": _derive_currency(metadata),
        # Optional fields — populated from metadata if stored, else omitted (None).
        "payouts30d": metadata.get("payouts30d") or None,
        "lastHrisSyncAt": metadata.get("lastHrisSyncAt") or metadata.get("last_hris_sync_at") or None,
    }


# ── bulk data fetchers ────────────────────────────────────────────────────────

def _fetch_all_tenants(conn) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, name, kind, metadata, is_active, created_at FROM tenants ORDER BY created_at DESC")
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
            "SELECT id, name, kind, metadata, is_active, created_at FROM tenants WHERE id = %s",
            (tenant_id,),
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
