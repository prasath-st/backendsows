"""
Tenant subscription + SSO config management.

Endpoints:
  GET    /api/admin/tenants/{tenantId}/subscription
  PATCH  /api/admin/tenants/{tenantId}/subscription
  POST   /api/admin/tenants/{tenantId}/subscription    (usage increment)
  GET    /api/admin/tenants/{tenantId}/subscription/history
  POST   /api/admin/tenants/{tenantId}/sso

All mutations audit → MongoDB.  Persistence → Neon Postgres via the
tenant_subscriptions, tenant_subscription_history, and tenant_sso_configs
tables (created idempotently on startup via init_superadmin_schema).

tenant_sso_configs uses the existing table shape (sso_config JSONB column,
slug UNIQUE, BIGSERIAL id primary key).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from psycopg2.extras import Json, RealDictCursor

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_admin

router = APIRouter(tags=["tenant-subscription"])


# ── Plan catalog (mirrors FE PLAN_CATALOG) ────────────────────────────────────

VALID_PLAN_CODES = {"trial", "pilot", "growth", "enterprise"}

PLAN_CATALOG: dict[str, dict] = {
    "trial": {
        "code": "trial",
        "label": "Trial",
        "description": "Evaluate Glimmora with core delivery workflows — time-boxed with caps.",
        "listPriceInr": 0,
        "features": ["sow.manual", "billing.workforce", "analytics.basic"],
        "limits": {
            "activeSows": 1,
            "activeProjects": 1,
            "seats": 2,
            "aiInvocationsMonth": 50,
            "auditRetentionDays": 7,
        },
    },
    "pilot": {
        "code": "pilot",
        "label": "Pilot",
        "description": "First production MOU — manual SOW + decomposition with moderate limits.",
        "listPriceInr": 49999,
        "features": [
            "sow.manual", "sow.ai_intake", "decomposition.ai",
            "billing.workforce", "billing.rate_cards", "analytics.basic",
        ],
        "limits": {
            "activeSows": 3,
            "activeProjects": 3,
            "seats": 5,
            "aiInvocationsMonth": 500,
            "auditRetentionDays": 30,
        },
    },
    "growth": {
        "code": "growth",
        "label": "Growth",
        "description": "Scaling delivery org — full SOW AI, reviewer hub, analytics, SSO.",
        "listPriceInr": 199999,
        "features": [
            "sow.manual", "sow.ai_intake", "decomposition.ai", "reviewer.hub",
            "billing.workforce", "billing.rate_cards", "analytics.basic",
            "analytics.full", "analytics.export", "compliance.module",
            "audit.export", "integrations.sso",
        ],
        "limits": {
            "activeSows": 15,
            "activeProjects": 10,
            "seats": 25,
            "aiInvocationsMonth": 5000,
            "auditRetentionDays": 90,
        },
    },
    "enterprise": {
        "code": "enterprise",
        "label": "Enterprise",
        "description": "Regulated and global programs — unlimited scale, all integrations, extended audit.",
        "listPriceInr": None,
        "features": [
            "sow.manual", "sow.ai_intake", "decomposition.ai", "reviewer.hub",
            "billing.workforce", "billing.rate_cards", "analytics.basic",
            "analytics.full", "analytics.export", "compliance.module",
            "audit.export", "integrations.sso", "integrations.hris", "integrations.webhooks",
        ],
        "limits": {
            "activeSows": None,
            "activeProjects": None,
            "seats": None,
            "aiInvocationsMonth": None,
            "auditRetentionDays": 365,
        },
    },
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def _get_subscription(tenant_id: str) -> dict[str, Any] | None:
    """Fetch the current subscription row for a tenant."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM tenant_subscriptions WHERE tenant_id = %s",
            (tenant_id,),
        )
        return cur.fetchone()


def _subscription_out(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a DB row into the TenantSubscriptionSnapshot shape the FE expects."""
    plan_code = row.get("plan_code") or "pilot"
    plan = PLAN_CATALOG.get(plan_code, PLAN_CATALOG["pilot"])

    usage_raw = row.get("usage_counters") or {}
    if isinstance(usage_raw, str):
        try:
            usage_raw = json.loads(usage_raw)
        except (ValueError, TypeError):
            usage_raw = {}

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    ai_period = usage_raw.get("aiInvocationsPeriod")
    usage = {
        "activeSows": usage_raw.get("activeSows", 0),
        "activeProjects": usage_raw.get("activeProjects", 0),
        "seats": usage_raw.get("seats", 1),
        "aiInvocationsMonth": (
            usage_raw.get("aiInvocationsMonth", 0)
            if ai_period == current_month
            else 0
        ),
    }

    trial_ends_at = row.get("trial_ends_at")
    trial_ends_str: str | None = None
    if isinstance(trial_ends_at, datetime):
        trial_ends_str = trial_ends_at.isoformat()
    elif isinstance(trial_ends_at, str):
        trial_ends_str = trial_ends_at

    trial_expired = False
    if plan_code == "trial" and trial_ends_str:
        try:
            trial_expired = datetime.fromisoformat(trial_ends_str.replace("Z", "+00:00")) < datetime.now(timezone.utc)
        except ValueError:
            pass

    enabled_features = ["sow.manual"] if trial_expired else list(plan.get("features", []))

    started_at = row.get("subscription_started_at")

    return {
        "tenantId": row["tenant_id"],
        "tenantName": row.get("tenant_name") or row["tenant_id"],
        "tenantSlug": row.get("tenant_slug") or row["tenant_id"],
        "tenantStatus": row.get("tenant_status") or "active",
        "plan": plan,
        "availablePlans": list(PLAN_CATALOG.values()),
        "subscriptionStartedAt": started_at.isoformat() if isinstance(started_at, datetime) else started_at,
        "trialEndsAt": trial_ends_str,
        "trialExpired": trial_expired,
        "usage": usage,
        "contractRef": row.get("contract_ref"),
        "enabledFeatures": enabled_features,
        "source": "db",
    }


def _redact_sso(cfg: Any) -> Any:
    """Strip clientSecret from SSO config before writing to audit."""
    if not cfg or not isinstance(cfg, dict):
        return cfg
    oidc = cfg.get("oidc")
    if oidc and isinstance(oidc, dict):
        return {**cfg, "oidc": {**oidc, "clientSecret": "[REDACTED]" if oidc.get("clientSecret") else None}}
    return cfg


# ── Pydantic models ───────────────────────────────────────────────────────────

class PatchSubscriptionBody(BaseModel):
    planCode: str
    contractRef: str | None = None
    trialDays: int | None = None


class UsageIncrementBody(BaseModel):
    metric: str
    delta: int = 1


class SsoConfigBody(BaseModel):
    enabled: bool
    kind: str           # "saml" | "oidc"
    # SAML fields
    entityId: str | None = None
    ssoUrl: str | None = None
    certificate: str | None = None
    attributeMap: dict[str, Any] | None = None
    # OIDC fields
    issuer: str | None = None
    clientId: str | None = None
    clientSecret: str | None = None
    scopes: list[str] | None = None


# ── GET /api/admin/tenants/{tenantId}/subscription ────────────────────────────

@router.get("/api/admin/tenants/{tenantId}/subscription")
async def get_subscription(
    tenantId: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    row = _get_subscription(tenantId)
    if row:
        return _subscription_out(row)
    # Fallback: return mock pilot shape for tenants not yet in DB
    return {
        "tenantId": tenantId,
        "tenantName": tenantId,
        "tenantSlug": tenantId,
        "tenantStatus": "active",
        "plan": PLAN_CATALOG["pilot"],
        "availablePlans": list(PLAN_CATALOG.values()),
        "subscriptionStartedAt": None,
        "trialEndsAt": None,
        "trialExpired": False,
        "usage": {"activeSows": 0, "activeProjects": 0, "seats": 1, "aiInvocationsMonth": 0},
        "contractRef": None,
        "enabledFeatures": list(PLAN_CATALOG["pilot"]["features"]),
        "source": "mock",
    }


# ── PATCH /api/admin/tenants/{tenantId}/subscription ─────────────────────────

@router.patch("/api/admin/tenants/{tenantId}/subscription")
async def patch_subscription(
    tenantId: str,
    body: PatchSubscriptionBody,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    if body.planCode not in VALID_PLAN_CODES:
        raise HTTPException(status_code=400, detail={"error": "invalid_body", "details": "planCode must be one of trial, pilot, growth, enterprise"})

    conn = _conn()
    existing = _get_subscription(tenantId)
    from_plan = existing["plan_code"] if existing else None
    now = datetime.now(timezone.utc)

    trial_ends_at = None
    if body.planCode == "trial" and body.trialDays:
        trial_ends_at = now + timedelta(days=int(body.trialDays))

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if existing:
            cur.execute(
                """
                UPDATE tenant_subscriptions
                   SET plan_code     = %s,
                       contract_ref  = COALESCE(%s, contract_ref),
                       trial_ends_at = COALESCE(%s, trial_ends_at),
                       updated_at    = now()
                 WHERE tenant_id = %s
                 RETURNING *
                """,
                (body.planCode, body.contractRef, trial_ends_at, tenantId),
            )
        else:
            cur.execute(
                """
                INSERT INTO tenant_subscriptions
                    (tenant_id, plan_code, contract_ref, trial_ends_at,
                     subscription_started_at, updated_at)
                VALUES (%s, %s, %s, %s, now(), now())
                RETURNING *
                """,
                (tenantId, body.planCode, body.contractRef, trial_ends_at),
            )
        row = cur.fetchone()

    # Record history entry
    history_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenant_subscription_history
                (id, tenant_id, from_plan, to_plan, changed_by_user_id,
                 changed_by_email, changed_by_role, source, contract_ref, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'admin', %s, now())
            """,
            (
                history_id, tenantId, from_plan, body.planCode,
                str(admin.get("id") or ""),
                admin.get("email") or "",
                admin.get("role") or "admin",
                body.contractRef,
            ),
        )
    conn.commit()

    # Audit → MongoDB
    write_audit(
        actor_id=str(admin.get("id") or ""),
        actor_email=admin.get("email"),
        actor_role=admin.get("role"),
        action="subscription.plan_changed",
        target="tenant_subscription",
        target_id=tenantId,
        details=f"{from_plan or 'none'} → {body.planCode}",
        service="superadmin-service",
        tenant_id=tenantId,
        ip_address=request.client.host if request.client else None,
        extra={
            "fromPlan": from_plan,
            "toPlan": body.planCode,
            "contractRef": body.contractRef,
            "source": "admin",
        },
    )

    return _subscription_out(row)


# ── POST /api/admin/tenants/{tenantId}/subscription (usage increment) ─────────

@router.post("/api/admin/tenants/{tenantId}/subscription")
async def increment_usage(
    tenantId: str,
    body: UsageIncrementBody,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    valid_metrics = {"activeSows", "activeProjects", "seats", "aiInvocationsMonth"}
    if body.metric not in valid_metrics:
        raise HTTPException(status_code=400, detail={"error": "metric_required"})

    conn = _conn()
    existing = _get_subscription(tenantId)
    if not existing:
        raise HTTPException(status_code=404, detail={"error": "tenant_not_found"})

    usage_raw = existing.get("usage_counters") or {}
    if isinstance(usage_raw, str):
        try:
            usage_raw = json.loads(usage_raw)
        except (ValueError, TypeError):
            usage_raw = {}

    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    usage = dict(usage_raw)

    if body.metric == "aiInvocationsMonth":
        if usage.get("aiInvocationsPeriod") != current_month:
            usage["aiInvocationsPeriod"] = current_month
            usage["aiInvocationsMonth"] = body.delta
        else:
            usage["aiInvocationsMonth"] = usage.get("aiInvocationsMonth", 0) + body.delta
    else:
        usage[body.metric] = usage.get(body.metric, 0) + body.delta

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE tenant_subscriptions
               SET usage_counters = %s, updated_at = now()
             WHERE tenant_id = %s
             RETURNING *
            """,
            (Json(usage), tenantId),
        )
        row = cur.fetchone()
    conn.commit()

    return _subscription_out(row)


# ── GET /api/admin/tenants/{tenantId}/subscription/history ────────────────────

@router.get("/api/admin/tenants/{tenantId}/subscription/history")
async def get_subscription_history(
    tenantId: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM tenant_subscription_history
             WHERE tenant_id = %s
             ORDER BY created_at DESC
             LIMIT 50
            """,
            (tenantId,),
        )
        rows = cur.fetchall()

    items = []
    for r in rows:
        created = r.get("created_at")
        items.append({
            "id": str(r["id"]),
            "at": created.isoformat() if isinstance(created, datetime) else str(created),
            "fromPlan": r.get("from_plan"),
            "toPlan": r.get("to_plan"),
            "changedByUserId": str(r.get("changed_by_user_id") or ""),
            "changedByName": r.get("changed_by_name"),
            "changedByRole": r.get("changed_by_role") or "admin",
            "source": r.get("source") or "admin",
            "contractRef": r.get("contract_ref"),
            "note": r.get("note"),
        })

    return {"tenantId": tenantId, "items": items}


# ── POST /api/admin/tenants/{tenantId}/sso ────────────────────────────────────

@router.post("/api/admin/tenants/{tenantId}/sso")
async def set_tenant_sso(
    tenantId: str,
    body: SsoConfigBody,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    kind = body.kind
    if kind not in ("saml", "oidc"):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_shape", "message": "kind must be 'saml' or 'oidc'"},
        )

    # Validate required fields per kind
    if kind == "saml":
        if not body.entityId or not body.ssoUrl or not body.certificate:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_saml_fields",
                    "message": "SAML config requires entityId, ssoUrl, and certificate",
                },
            )
        saml_cfg: dict[str, Any] | None = {
            "entityId": body.entityId,
            "ssoUrl": body.ssoUrl,
            "certificate": body.certificate,
            "attributeMap": body.attributeMap or {"email": "email"},
        }
        oidc_cfg: dict[str, Any] | None = None
    else:  # oidc
        if not body.issuer or not body.clientId or not body.clientSecret:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "missing_oidc_fields",
                    "message": "OIDC config requires issuer, clientId, and clientSecret",
                },
            )
        saml_cfg = None
        oidc_cfg = {
            "issuer": body.issuer,
            "clientId": body.clientId,
            "clientSecret": body.clientSecret,
            "scopes": body.scopes or ["openid", "profile", "email"],
        }

    # Full config JSON — matches TenantSsoConfig shape from FE lib/sso/types.ts
    config_json = {
        "enabled": body.enabled,
        "kind": kind,
        "saml": saml_cfg,
        "oidc": oidc_cfg,
    }

    # Redacted version for audit (never log clientSecret)
    audit_after: dict[str, Any] = {
        "enabled": body.enabled,
        "kind": kind,
        "saml": saml_cfg,
        "oidc": (
            {**oidc_cfg, "clientSecret": "[REDACTED]"}
            if oidc_cfg
            else None
        ),
    }

    conn = _conn()

    # Fetch previous config for audit before snapshot
    # tenant_sso_configs uses: id(bigserial), tenant_id(UNIQUE), slug(UNIQUE),
    #   enabled, kind, default_role, sso_config(JSONB), created_at, updated_at
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT sso_config FROM tenant_sso_configs WHERE tenant_id = %s",
            (tenantId,),
        )
        prev_row = cur.fetchone()

    prev_config: Any = None
    if prev_row:
        prev_raw = prev_row.get("sso_config")
        if isinstance(prev_raw, str):
            try:
                prev_config = json.loads(prev_raw)
            except (ValueError, TypeError):
                prev_config = prev_raw
        else:
            prev_config = prev_raw

    # Upsert SSO config — ON CONFLICT on tenant_id (which has UNIQUE constraint).
    # slug is UNIQUE NOT NULL, so we derive a slug from tenantId when inserting.
    slug = tenantId.lower().replace(" ", "-")[:100]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tenant_sso_configs
                (tenant_id, slug, enabled, kind, sso_config, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (tenant_id) DO UPDATE
               SET kind       = EXCLUDED.kind,
                   enabled    = EXCLUDED.enabled,
                   sso_config = EXCLUDED.sso_config,
                   updated_at = now()
            """,
            (tenantId, slug, body.enabled, kind, Json(config_json)),
        )
    conn.commit()

    # Audit → MongoDB
    write_audit(
        actor_id=str(admin.get("id") or ""),
        actor_email=admin.get("email"),
        actor_role=admin.get("role"),
        action="tenant.sso.configure",
        target="tenant",
        target_id=tenantId,
        details=f"SSO kind={kind} enabled={body.enabled}",
        service="superadmin-service",
        tenant_id=tenantId,
        ip_address=request.client.host if request.client else None,
        extra={
            "kind": kind,
            "enabled": body.enabled,
            "before": {"ssoConfig": _redact_sso(prev_config)},
            "after": {"ssoConfig": audit_after},
        },
    )

    return {
        "ok": True,
        "tenantId": tenantId,
        "kind": kind,
        "enabled": body.enabled,
    }
