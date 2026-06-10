"""KYC review queue for the Super Admin (Trust & Safety).

Student / Women applicants are flagged for KYC; this surface lets the platform
admin list pending submissions and approve/reject them. Backed by the shared
`contributor_kyc` table (written by contributor-service).

Endpoints (Super Admin):
  GET   /api/superadmin/kyc                      — list (filter by status)
  GET   /api/superadmin/kyc/{account_id}         — one submission
  POST  /api/superadmin/kyc/{account_id}/decision  body {decision: approve|reject, note?}
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from psycopg2.extras import Json, RealDictCursor

from shared.audit import write_audit
from shared.db import get_pg_connection
from shared.deps import get_current_admin
from shared.mailer import send_email
from shared.config import settings

router = APIRouter(tags=["superadmin-kyc"])


def _row_out(r: dict) -> dict[str, Any]:
    data = r.get("data") or {}
    if isinstance(data, str):
        import json as _json
        try:
            data = _json.loads(data)
        except (ValueError, TypeError):
            data = {}
    return {
        "accountId": r["account_id"],
        "email": r.get("email"),
        "name": (f"{r.get('first_name','') or ''} {r.get('last_name','') or ''}".strip()
                 or r.get("email")),
        "role": r.get("role"),
        "segment": data.get("segment") or r.get("segment"),
        "status": r.get("status"),
        "data": data,
        "updatedAt": r["updated_at"].isoformat() if r.get("updated_at") else None,
    }


@router.get("/api/superadmin/kyc")
async def list_kyc(
    admin: Annotated[dict, Depends(get_current_admin)],
    status: str | None = None,
):
    """List KYC submissions joined with the account, newest first. Default shows
    everything; pass ?status=pending to see only the review queue."""
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        q = (
            "SELECT k.account_id, k.status, k.data, k.updated_at, "
            "       a.email, a.first_name, a.last_name, a.role "
            "  FROM contributor_kyc k "
            "  LEFT JOIN login_accounts a ON a.id = k.account_id "
        )
        params: list[Any] = []
        if status:
            q += " WHERE k.status = %s"
            params.append(status)
        q += " ORDER BY k.updated_at DESC"
        cur.execute(q, tuple(params))
        items = [_row_out(r) for r in cur.fetchall()]
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) FROM contributor_kyc GROUP BY status")
        for st, n in cur.fetchall():
            counts[st or "unknown"] = int(n)
    return {"items": items, "total": len(items), "counts": counts}


@router.get("/api/superadmin/kyc/{account_id}")
async def get_kyc(account_id: int, admin: Annotated[dict, Depends(get_current_admin)]):
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT k.account_id, k.status, k.data, k.updated_at, "
            "       a.email, a.first_name, a.last_name, a.role "
            "  FROM contributor_kyc k LEFT JOIN login_accounts a ON a.id = k.account_id "
            " WHERE k.account_id = %s",
            (account_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="KYC submission not found")
    return _row_out(row)


class KycDecision(BaseModel):
    decision: str  # 'approve' | 'reject'
    note: str | None = None


@router.post("/api/superadmin/kyc/{account_id}/decision")
async def decide_kyc(
    account_id: int,
    body: KycDecision,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    new_status = "verified" if body.decision in ("approve", "approved", "verify") else "rejected"
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT account_id, data FROM contributor_kyc WHERE account_id = %s", (account_id,))
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="KYC submission not found")
        data = dict(existing.get("data") or {})
        data["reviewNote"] = body.note
        data["reviewedBy"] = admin.get("email")
        cur.execute(
            "UPDATE contributor_kyc SET status = %s, data = %s, updated_at = now() WHERE account_id = %s",
            (new_status, Json(data), account_id))
        # On approval, also clear the account's approval gate so they get full
        # access (women/students were held 'pending' until KYC clears).
        if new_status == "verified":
            cur.execute(
                "UPDATE login_accounts SET approval_status = 'approved', updated_at = now() WHERE id = %s",
                (account_id,))
        elif new_status == "rejected":
            cur.execute(
                "UPDATE login_accounts SET approval_status = 'rejected', updated_at = now() WHERE id = %s",
                (account_id,))
        cur.execute("SELECT email, first_name FROM login_accounts WHERE id = %s", (account_id,))
        acct = cur.fetchone()
    conn.commit()

    # Notify the applicant.
    if acct and acct.get("email"):
        login_url = f"{settings.frontend_base_url}/contributor/login"
        if new_status == "verified":
            send_email(to_email=acct["email"], subject="Your Glimmora verification is approved",
                       body=f"Good news! Your identity verification was approved.\n\nSign in: {login_url}",
                       category="kyc_approved")
        else:
            send_email(to_email=acct["email"], subject="Update on your Glimmora verification",
                       body="Your identity verification was not approved. Please contact support.",
                       category="kyc_rejected")

    write_audit(actor_id=str(admin.get("id", "")), actor_email=admin.get("email"),
                actor_role=admin.get("role"), action=f"kyc_{new_status}",
                target="contributor_kyc", target_id=str(account_id),
                service="superadmin-service",
                ip_address=request.client.host if request.client else None)
    return {"ok": True, "accountId": account_id, "status": new_status}
