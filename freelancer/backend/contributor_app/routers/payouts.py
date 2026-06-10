"""
Payouts module — /api/v1/payouts/**

SIMULATED money-movement: payout requests and status transitions are persisted
in Postgres (payouts, payout_methods tables) but no real Razorpay/bank API is
called. Mutations write audit events to MongoDB via shared.audit.write_audit.

Tables owned here:
  payouts          — individual payout records per contributor
  payout_methods   — saved payout methods (bank / UPI / etc.)

Mounted at /api/v1 prefix so FE routes (/api/v1/payouts/...) resolve directly.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from psycopg2.extras import Json

from shared.audit import write_audit
from shared.deps import get_current_user
from contributor_app import db

router = APIRouter(prefix="/api/v1", tags=["payouts"])

# ── Schema DDL (idempotent, called from init_contributor_schema) ──────────────

PAYOUTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS payouts (
    id            TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    account_id    BIGINT NOT NULL,
    task_id       TEXT,
    task_title    TEXT DEFAULT '',
    amount_minor  BIGINT NOT NULL DEFAULT 0,
    currency      TEXT NOT NULL DEFAULT 'INR',
    status        TEXT NOT NULL DEFAULT 'eligible',
    -- status lifecycle: eligible -> pending -> paid | failed -> retry -> pending | reversed
    eligible_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at       TIMESTAMPTZ,
    external_ref  TEXT,
    failure_reason TEXT,
    method_id     TEXT,
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_payouts_account ON payouts(account_id);
CREATE INDEX IF NOT EXISTS idx_payouts_status  ON payouts(status);

CREATE TABLE IF NOT EXISTS payout_methods (
    id            TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    account_id    BIGINT NOT NULL,
    type          TEXT NOT NULL DEFAULT 'bank',  -- bank | upi | razorpay
    label         TEXT NOT NULL DEFAULT '',
    is_default    BOOLEAN NOT NULL DEFAULT FALSE,
    verified_at   TIMESTAMPTZ,
    data          JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_payout_methods_account ON payout_methods(account_id);
"""


def init_payouts_schema() -> None:
    """Create payouts + payout_methods tables idempotently. Called from app startup."""
    c = db.conn()
    with c.cursor() as cur:
        cur.execute(PAYOUTS_SCHEMA_SQL)
    c.commit()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _acting_id(
    user: Annotated[dict, Depends(get_current_user)],
    x_contributor_id: Annotated[str | None, Header(alias="X-Contributor-Id")] = None,
) -> int:
    raw = x_contributor_id or user.get("id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid contributor id")


ActId = Annotated[int, Depends(_acting_id)]


def _acting_user(
    user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    return user


ActUser = Annotated[dict, Depends(_acting_user)]


def _require_row(row: dict | None, what: str = "Resource") -> dict:
    if row is None:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return row


# ── Row serialiser ────────────────────────────────────────────────────────────

def _ser(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Serialise a payout/method row: datetimes → iso, merge data JSONB."""
    if row is None:
        return None
    out: dict[str, Any] = {}
    nested = row.get("data") if isinstance(row.get("data"), dict) else {}
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
    for k, v in (nested or {}).items():
        out.setdefault(k, v)
    return out


def _fetch_payout(payout_id: str, account_id: int) -> dict | None:
    return _ser(db.fetch_one(
        "SELECT * FROM payouts WHERE id=%s AND account_id=%s",
        (payout_id, account_id),
    ))


def _fetch_method(method_id: str, account_id: int) -> dict | None:
    return _ser(db.fetch_one(
        "SELECT * FROM payout_methods WHERE id=%s AND account_id=%s",
        (method_id, account_id),
    ))


# ════════════════════════════════════════════════════════════════════════════
# GET /api/v1/payouts
# Paged list of payouts for the authenticated contributor.
# Query params: page, limit, status
# Response: { items, total, page, limit }
# ════════════════════════════════════════════════════════════════════════════

@router.get("/payouts")
async def list_payouts(
    account_id: ActId,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=25, ge=1, le=100),
    status: str | None = None,
):
    where = "account_id=%s"
    params: list[Any] = [account_id]
    if status:
        where += " AND status=%s"
        params.append(status)
    rows = db.fetch_all(
        f"SELECT * FROM payouts WHERE {where} ORDER BY created_at DESC",
        tuple(params),
    )
    items = [_ser(r) for r in rows]
    # Attach project name from data if stored
    total = len(items)
    start = (page - 1) * limit
    return {
        "items": items[start : start + limit],
        "total": total,
        "page": page,
        "limit": limit,
    }


# ════════════════════════════════════════════════════════════════════════════
# POST /api/v1/payouts/{payoutId}/request
# Transition: eligible → pending  (contributor requests withdrawal)
# ════════════════════════════════════════════════════════════════════════════

@router.post("/payouts/{payout_id}/request")
async def request_payout(
    payout_id: str,
    account_id: ActId,
    user: ActUser,
    payload: dict = Body(default={}),
):
    row = _require_row(
        _ser(db.fetch_one("SELECT * FROM payouts WHERE id=%s AND account_id=%s", (payout_id, account_id))),
        "Payout",
    )
    if row["status"] != "eligible":
        raise HTTPException(
            status_code=409,
            detail=f"Payout is {row['status']}; only eligible payouts can be requested",
        )
    method_id = payload.get("methodId") or row.get("method_id")
    updated = db.execute(
        "UPDATE payouts SET status='pending', method_id=%s, updated_at=now() "
        "WHERE id=%s AND account_id=%s RETURNING *",
        (method_id, payout_id, account_id),
    )
    write_audit(
        actor_id=str(account_id),
        actor_email=user.get("email"),
        actor_role=user.get("role", "contributor"),
        action="payout.request",
        target="payout",
        target_id=payout_id,
        details=f"status eligible→pending; method={method_id}",
        service="contributor-service",
    )
    return _ser(updated)


# ════════════════════════════════════════════════════════════════════════════
# POST /api/v1/payouts/{payoutId}/hold
# Transition: pending → on_hold  (admin places hold)
# ════════════════════════════════════════════════════════════════════════════

@router.post("/payouts/{payout_id}/hold")
async def hold_payout(
    payout_id: str,
    account_id: ActId,
    user: ActUser,
    payload: dict = Body(default={}),
):
    row = _require_row(
        _ser(db.fetch_one("SELECT * FROM payouts WHERE id=%s AND account_id=%s", (payout_id, account_id))),
        "Payout",
    )
    if row["status"] not in ("pending", "eligible"):
        raise HTTPException(
            status_code=409,
            detail=f"Payout is {row['status']}; cannot place hold",
        )
    reason = payload.get("reason", "")
    updated = db.execute(
        "UPDATE payouts SET status='on_hold', "
        "data = data || %s, updated_at=now() "
        "WHERE id=%s AND account_id=%s RETURNING *",
        (Json({"hold_reason": reason}), payout_id, account_id),
    )
    write_audit(
        actor_id=str(account_id),
        actor_email=user.get("email"),
        actor_role=user.get("role", "contributor"),
        action="payout.hold",
        target="payout",
        target_id=payout_id,
        details=f"reason={reason}",
        service="contributor-service",
    )
    return _ser(updated)


# ════════════════════════════════════════════════════════════════════════════
# POST /api/v1/payouts/{payoutId}/release-hold
# Transition: on_hold → pending
# ════════════════════════════════════════════════════════════════════════════

@router.post("/payouts/{payout_id}/release-hold")
async def release_hold_payout(
    payout_id: str,
    account_id: ActId,
    user: ActUser,
):
    row = _require_row(
        _ser(db.fetch_one("SELECT * FROM payouts WHERE id=%s AND account_id=%s", (payout_id, account_id))),
        "Payout",
    )
    if row["status"] != "on_hold":
        raise HTTPException(
            status_code=409,
            detail=f"Payout is {row['status']}; not on hold",
        )
    updated = db.execute(
        "UPDATE payouts SET status='pending', updated_at=now() "
        "WHERE id=%s AND account_id=%s RETURNING *",
        (payout_id, account_id),
    )
    write_audit(
        actor_id=str(account_id),
        actor_email=user.get("email"),
        actor_role=user.get("role", "contributor"),
        action="payout.release_hold",
        target="payout",
        target_id=payout_id,
        service="contributor-service",
    )
    return _ser(updated)


# ════════════════════════════════════════════════════════════════════════════
# POST /api/v1/payouts/{payoutId}/retry
# Transition: failed → pending  (contributor retries a failed payout)
# ════════════════════════════════════════════════════════════════════════════

@router.post("/payouts/{payout_id}/retry")
async def retry_payout(
    payout_id: str,
    account_id: ActId,
    user: ActUser,
    payload: dict = Body(default={}),
):
    row = _require_row(
        _ser(db.fetch_one("SELECT * FROM payouts WHERE id=%s AND account_id=%s", (payout_id, account_id))),
        "Payout",
    )
    if row["status"] != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Payout is {row['status']}; only failed payouts can be retried",
        )
    method_id = payload.get("methodId") or row.get("method_id")
    updated = db.execute(
        "UPDATE payouts SET status='pending', method_id=%s, failure_reason=NULL, "
        "updated_at=now() "
        "WHERE id=%s AND account_id=%s RETURNING *",
        (method_id, payout_id, account_id),
    )
    write_audit(
        actor_id=str(account_id),
        actor_email=user.get("email"),
        actor_role=user.get("role", "contributor"),
        action="payout.retry",
        target="payout",
        target_id=payout_id,
        details=f"method={method_id}",
        service="contributor-service",
    )
    return _ser(updated)


# ════════════════════════════════════════════════════════════════════════════
# GET /api/v1/payouts/methods
# List payout methods for the contributor.
# ════════════════════════════════════════════════════════════════════════════

@router.get("/payouts/methods")
async def list_payout_methods(account_id: ActId):
    rows = db.fetch_all(
        "SELECT * FROM payout_methods WHERE account_id=%s ORDER BY is_default DESC, created_at ASC",
        (account_id,),
    )
    return {"items": [_ser(r) for r in rows]}


# ════════════════════════════════════════════════════════════════════════════
# POST /api/v1/payouts/methods
# Add a new payout method.
# Body: { type, label, ifsc?, country?, currency?, ...extra }
# ════════════════════════════════════════════════════════════════════════════

@router.post("/payouts/methods")
async def add_payout_method(
    account_id: ActId,
    user: ActUser,
    payload: dict = Body(default={}),
):
    method_type = payload.get("type", "bank")
    label = payload.get("label", "")
    if not label:
        raise HTTPException(status_code=422, detail="label is required")

    # If this is the first method, make it default automatically
    existing_count = db.fetch_one(
        "SELECT COUNT(*) AS c FROM payout_methods WHERE account_id=%s",
        (account_id,),
    )
    is_default = (int((existing_count or {}).get("c", 0)) == 0)

    # Extra fields stored in JSONB data
    extra = {k: v for k, v in payload.items() if k not in ("type", "label")}
    verified_at = None
    if payload.get("verifiedAt"):
        verified_at = payload["verifiedAt"]

    row = db.execute(
        """
        INSERT INTO payout_methods
            (account_id, type, label, is_default, verified_at, data)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (account_id, method_type, label, is_default, verified_at, Json(extra)),
    )
    write_audit(
        actor_id=str(account_id),
        actor_email=user.get("email"),
        actor_role=user.get("role", "contributor"),
        action="payout_method.add",
        target="payout_method",
        target_id=str((row or {}).get("id", "")),
        details=f"type={method_type}, label={label}",
        service="contributor-service",
    )
    return _ser(row)


# ════════════════════════════════════════════════════════════════════════════
# PATCH /api/v1/payouts/methods/{methodId}
# Set as default or update label/details.
# Body: { setDefault?: bool, label?: str, ...extra }
# ════════════════════════════════════════════════════════════════════════════

@router.patch("/payouts/methods/{method_id}")
async def update_payout_method(
    method_id: str,
    account_id: ActId,
    user: ActUser,
    payload: dict = Body(default={}),
):
    _require_row(_fetch_method(method_id, account_id), "Payout method")

    if payload.get("setDefault") or payload.get("set_default"):
        # Clear existing default, then set this one
        c = db.conn()
        with c.cursor() as cur:
            cur.execute(
                "UPDATE payout_methods SET is_default=FALSE WHERE account_id=%s",
                (account_id,),
            )
            cur.execute(
                "UPDATE payout_methods SET is_default=TRUE, updated_at=now() "
                "WHERE id=%s AND account_id=%s",
                (method_id, account_id),
            )
        c.commit()

    if "label" in payload:
        db.execute(
            "UPDATE payout_methods SET label=%s, updated_at=now() WHERE id=%s AND account_id=%s",
            (payload["label"], method_id, account_id),
        )

    write_audit(
        actor_id=str(account_id),
        actor_email=user.get("email"),
        actor_role=user.get("role", "contributor"),
        action="payout_method.update",
        target="payout_method",
        target_id=method_id,
        details=str(payload),
        service="contributor-service",
    )
    updated = _require_row(_fetch_method(method_id, account_id), "Payout method")
    return updated


# ════════════════════════════════════════════════════════════════════════════
# DELETE /api/v1/payouts/methods/{methodId}
# Remove a saved payout method.
# ════════════════════════════════════════════════════════════════════════════

@router.delete("/payouts/methods/{method_id}")
async def delete_payout_method(
    method_id: str,
    account_id: ActId,
    user: ActUser,
):
    row = _require_row(_fetch_method(method_id, account_id), "Payout method")
    db.execute(
        "DELETE FROM payout_methods WHERE id=%s AND account_id=%s",
        (method_id, account_id),
    )
    write_audit(
        actor_id=str(account_id),
        actor_email=user.get("email"),
        actor_role=user.get("role", "contributor"),
        action="payout_method.delete",
        target="payout_method",
        target_id=method_id,
        service="contributor-service",
    )
    return {"deleted": True, "id": method_id}


# ════════════════════════════════════════════════════════════════════════════
# GET /api/v1/payouts/tenant
# Tenant-wide payouts list (enterprise / admin view).
# Query params: status, page, limit, account_id (filter by contributor)
# ════════════════════════════════════════════════════════════════════════════

@router.get("/payouts/tenant")
async def list_tenant_payouts(
    user: ActUser,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=25, ge=1, le=100),
    contributor_id: int | None = None,
):
    # Tenant-wide endpoint: accessible by admin/enterprise roles; contributors
    # can only see their own (enforced by contributor_id param being set to
    # their own account or left None which returns all — let callers gate this).
    where_parts = ["1=1"]
    params: list[Any] = []

    if status:
        where_parts.append("status=%s")
        params.append(status)
    if contributor_id is not None:
        where_parts.append("account_id=%s")
        params.append(contributor_id)

    where = " AND ".join(where_parts)
    rows = db.fetch_all(
        f"SELECT * FROM payouts WHERE {where} ORDER BY created_at DESC",
        tuple(params),
    )
    items = [_ser(r) for r in rows]
    total = len(items)
    start = (page - 1) * limit
    return {
        "items": items[start : start + limit],
        "total": total,
        "page": page,
        "limit": limit,
    }
