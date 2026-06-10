"""
Payment rails management endpoints for the Super Admin dashboard.

GET  /api/payment-rails              — list all payment rails
GET  /api/payment-rails/{railId}     — single rail detail
PATCH /api/payment-rails/{railId}/status        — update status
PATCH /api/payment-rails/{railId}/hold-policy   — update hold policy
POST  /api/payment-rails/{railId}/rotate-key    — rotate secret key
POST  /api/payment-rails/{railId}/drain         — drain pending payouts

Shape: MockPaymentRail from newfrontend/frontend/src/mocks/admin/rails.ts
  {id, provider, country, currency, method, status, errorRate1hPct, keyMask,
   secretRotatedAt, webhookUrl, holdPolicy, pendingPayoutsCount,
   pendingPayoutsTotal, pendingPayoutsOldest}

All mutations are audited via shared.audit.write_audit.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["superadmin-payment-rails"])


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


def _row_to_out(row: dict[str, Any]) -> dict[str, Any]:
    """Map a payment_rails DB row to the MockPaymentRail shape."""
    return {
        "id": row["id"],
        "provider": row["provider"],
        "country": row["country"],
        "currency": row["currency"],
        "method": row["method"],
        "status": row["status"],
        "errorRate1hPct": float(row.get("error_rate_1h_pct") or 0.0),
        "keyMask": row.get("key_mask") or "****0000",
        "secretRotatedAt": _iso(row.get("secret_rotated_at")) or "",
        "webhookUrl": row.get("webhook_url") or "",
        "holdPolicy": row.get("hold_policy") or "hold_when_degraded",
        "pendingPayoutsCount": int(row.get("pending_payouts_count") or 0),
        "pendingPayoutsTotal": row.get("pending_payouts_total") or "₹0",
        "pendingPayoutsOldest": row.get("pending_payouts_oldest") or "",
    }


# ── schema init ───────────────────────────────────────────────────────────────

_SEED_ROWS = [
    {
        "id": "rail-rzp-neft",
        "provider": "Razorpay",
        "country": "India",
        "currency": "INR",
        "method": "NEFT",
        "status": "active",
        "error_rate_1h_pct": 0.0,
        "key_mask": "****8211",
        "secret_rotated_at": "2026-03-26T00:00:00+00:00",
        "webhook_url": "https://api.glimmora.app/rails/razorpay-neft",
        "hold_policy": "hold_when_degraded",
        "pending_payouts_count": 4,
        "pending_payouts_total": "₹18,200",
        "pending_payouts_oldest": "12 min ago",
    },
    {
        "id": "rail-rzp-upi",
        "provider": "Razorpay",
        "country": "India",
        "currency": "INR",
        "method": "UPI",
        "status": "degraded",
        "error_rate_1h_pct": 4.2,
        "key_mask": "****1234",
        "secret_rotated_at": "2026-03-26T00:00:00+00:00",
        "webhook_url": "https://api.glimmora.app/rails/razorpay-upi",
        "hold_policy": "hold_when_degraded",
        "pending_payouts_count": 42,
        "pending_payouts_total": "₹68,400",
        "pending_payouts_oldest": "1h ago",
    },
    {
        "id": "rail-rzp-wallet",
        "provider": "Razorpay",
        "country": "India",
        "currency": "INR",
        "method": "Wallet",
        "status": "active",
        "error_rate_1h_pct": 0.1,
        "key_mask": "****5599",
        "secret_rotated_at": "2026-03-26T00:00:00+00:00",
        "webhook_url": "https://api.glimmora.app/rails/razorpay-wallet",
        "hold_policy": "hold_when_degraded",
        "pending_payouts_count": 1,
        "pending_payouts_total": "₹2,400",
        "pending_payouts_oldest": "8 min ago",
    },
    {
        "id": "rail-wise",
        "provider": "Wise",
        "country": "Global",
        "currency": "Multi",
        "method": "SWIFT/SEPA",
        "status": "active",
        "error_rate_1h_pct": 0.0,
        "key_mask": "****2740",
        "secret_rotated_at": "2026-02-04T00:00:00+00:00",
        "webhook_url": "https://api.glimmora.app/rails/wise",
        "hold_policy": "hold_when_degraded",
        "pending_payouts_count": 6,
        "pending_payouts_total": "$11,800",
        "pending_payouts_oldest": "21 min ago",
    },
]


def init_payment_rails_schema() -> None:
    """Create the payment_rails table and seed mock data. Idempotent."""
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_rails (
                id                      TEXT PRIMARY KEY,
                provider                TEXT NOT NULL,
                country                 TEXT NOT NULL DEFAULT '',
                currency                TEXT NOT NULL DEFAULT 'INR',
                method                  TEXT NOT NULL DEFAULT '',
                status                  TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'degraded', 'paused')),
                error_rate_1h_pct       NUMERIC(6, 2) NOT NULL DEFAULT 0.0,
                key_mask                TEXT NOT NULL DEFAULT '',
                secret_rotated_at       TIMESTAMPTZ,
                webhook_url             TEXT NOT NULL DEFAULT '',
                hold_policy             TEXT NOT NULL DEFAULT 'hold_when_degraded'
                    CHECK (hold_policy IN ('hold_when_degraded', 'continue_routing')),
                pending_payouts_count   INTEGER NOT NULL DEFAULT 0,
                pending_payouts_total   TEXT NOT NULL DEFAULT '₹0',
                pending_payouts_oldest  TEXT NOT NULL DEFAULT '',
                created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        # Seed from mock data (idempotent)
        for row in _SEED_ROWS:
            cur.execute(
                """
                INSERT INTO payment_rails (
                    id, provider, country, currency, method, status,
                    error_rate_1h_pct, key_mask, secret_rotated_at, webhook_url,
                    hold_policy, pending_payouts_count, pending_payouts_total,
                    pending_payouts_oldest
                ) VALUES (
                    %(id)s, %(provider)s, %(country)s, %(currency)s, %(method)s, %(status)s,
                    %(error_rate_1h_pct)s, %(key_mask)s, %(secret_rotated_at)s, %(webhook_url)s,
                    %(hold_policy)s, %(pending_payouts_count)s, %(pending_payouts_total)s,
                    %(pending_payouts_oldest)s
                ) ON CONFLICT (id) DO NOTHING
                """,
                row,
            )
    conn.commit()
    logger.info("payment_rails schema + seed complete")


# ── request models ────────────────────────────────────────────────────────────

class _StatusBody(BaseModel):
    status: str


class _HoldPolicyBody(BaseModel):
    holdPolicy: str


# ── GET /api/payment-rails ────────────────────────────────────────────────────

@router.get("/api/payment-rails")
async def list_payment_rails(
    admin: Annotated[dict, Depends(get_current_admin)],
) -> list[dict[str, Any]]:
    """Return all payment rails in MockPaymentRail shape."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM payment_rails ORDER BY provider, method")
        rows = cur.fetchall()
    return [_row_to_out(dict(r)) for r in rows]


# ── GET /api/payment-rails/{railId} ──────────────────────────────────────────

@router.get("/api/payment-rails/{railId}")
async def get_payment_rail(
    railId: str,
    admin: Annotated[dict, Depends(get_current_admin)],
) -> dict[str, Any]:
    """Return a single payment rail by ID."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM payment_rails WHERE id = %s", (railId,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "rail_not_found", "railId": railId})
    return _row_to_out(dict(row))


# ── PATCH /api/payment-rails/{railId}/status ─────────────────────────────────

@router.patch("/api/payment-rails/{railId}/status")
async def update_rail_status(
    railId: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _StatusBody = Body(...),
) -> dict[str, Any]:
    """Set the status of a payment rail (active | degraded | paused)."""
    allowed = {"active", "degraded", "paused"}
    if body.status not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(allowed)}",
        )
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "UPDATE payment_rails SET status = %s, updated_at = NOW() "
            "WHERE id = %s RETURNING *",
            (body.status, railId),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "rail_not_found", "railId": railId})
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"),
            actor_email=admin.get("email"),
            actor_role=admin.get("role"),
            action="update_payment_rail_status",
            target=f"{row['provider']} {row['method']}",
            target_id=railId,
            details=f"status → {body.status}",
        )
    except Exception:  # noqa: BLE001
        pass

    return _row_to_out(dict(row))


# ── PATCH /api/payment-rails/{railId}/hold-policy ────────────────────────────

@router.patch("/api/payment-rails/{railId}/hold-policy")
async def update_hold_policy(
    railId: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _HoldPolicyBody = Body(...),
) -> dict[str, Any]:
    """Set the hold policy for a payment rail."""
    allowed = {"hold_when_degraded", "continue_routing"}
    if body.holdPolicy not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"holdPolicy must be one of {sorted(allowed)}",
        )
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "UPDATE payment_rails SET hold_policy = %s, updated_at = NOW() "
            "WHERE id = %s RETURNING *",
            (body.holdPolicy, railId),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "rail_not_found", "railId": railId})
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"),
            actor_email=admin.get("email"),
            actor_role=admin.get("role"),
            action="update_payment_rail_hold_policy",
            target=f"{row['provider']} {row['method']}",
            target_id=railId,
            details=f"holdPolicy → {body.holdPolicy}",
        )
    except Exception:  # noqa: BLE001
        pass

    return _row_to_out(dict(row))


# ── POST /api/payment-rails/{railId}/rotate-key ──────────────────────────────

@router.post("/api/payment-rails/{railId}/rotate-key")
async def rotate_rail_key(
    railId: str,
    admin: Annotated[dict, Depends(get_current_admin)],
) -> dict[str, Any]:
    """Rotate the secret key for a payment rail.

    Generates a new random suffix, updates key_mask and secret_rotated_at.
    In production this would call the provider's key-rotation API.
    """
    # Generate a masked key: ****XXXX (last 4 hex chars of a new random token)
    new_suffix = secrets.token_hex(2).upper()  # 4 hex chars
    new_key_mask = f"****{new_suffix}"
    now = datetime.now(timezone.utc)

    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "UPDATE payment_rails "
            "SET key_mask = %s, secret_rotated_at = %s, updated_at = NOW() "
            "WHERE id = %s RETURNING *",
            (new_key_mask, now, railId),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "rail_not_found", "railId": railId})
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"),
            actor_email=admin.get("email"),
            actor_role=admin.get("role"),
            action="rotate_payment_rail_key",
            target=f"{row['provider']} {row['method']}",
            target_id=railId,
            details=f"keyMask → {new_key_mask}",
        )
    except Exception:  # noqa: BLE001
        pass

    return _row_to_out(dict(row))


# ── POST /api/payment-rails/{railId}/drain ───────────────────────────────────

@router.post("/api/payment-rails/{railId}/drain")
async def drain_payment_rail(
    railId: str,
    admin: Annotated[dict, Depends(get_current_admin)],
) -> dict[str, Any]:
    """Drain all pending payouts from a rail (zero out pending counters).

    Also sets status to 'paused' so no new payouts are routed to this rail.
    In production this would trigger actual payout rerouting logic.
    """
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "UPDATE payment_rails "
            "SET pending_payouts_count = 0, "
            "    pending_payouts_total = '₹0', "
            "    pending_payouts_oldest = '', "
            "    status = 'paused', "
            "    updated_at = NOW() "
            "WHERE id = %s RETURNING *",
            (railId,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "rail_not_found", "railId": railId})
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"),
            actor_email=admin.get("email"),
            actor_role=admin.get("role"),
            action="drain_payment_rail",
            target=f"{row['provider']} {row['method']}",
            target_id=railId,
            details="Drained pending payouts; status set to paused",
        )
    except Exception:  # noqa: BLE001
        pass

    return _row_to_out(dict(row))
