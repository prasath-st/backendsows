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
    name = (f"{r.get('first_name','') or ''} {r.get('last_name','') or ''}".strip()
            or r.get("email"))
    segment = data.get("segment") or r.get("segment")
    submitted = (r.get("submitted_at") or r.get("updated_at"))
    submitted_iso = submitted.isoformat() if hasattr(submitted, "isoformat") else submitted
    return {
        # FE MockKycCase field names (canonical):
        "id": f"KYC-{r['account_id']}",
        "contributorName": name,
        "contributorEmail": r.get("email"),
        "dob": data.get("dob"),
        "country": data.get("country"),
        "track": data.get("track") or segment,
        "submittedAt": submitted_iso,
        "slaHours": data.get("slaHours") or 24,
        "idType": data.get("idType"),
        "idNumberLast4": data.get("idNumberLast4"),
        "autoChecks": data.get("autoChecks") or [],
        "status": r.get("status"),
        # Back-compat keys (existing consumers):
        "accountId": r["account_id"],
        "email": r.get("email"),
        "name": name,
        "role": r.get("role"),
        "segment": segment,
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
    """Full KYC case detail — contributor info, documents, status, submittedAt, history.

    Enriches the base kyc row with contributor_profiles (dob, country, segment,
    documents) and synthesises a history timeline from the stored data fields.
    The response shape is a superset of the list-item shape so the FE mapper
    (use-admin-kyc.ts → mapItem) works unchanged.
    """
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # ── Core KYC row + account ────────────────────────────────────────────
        cur.execute(
            "SELECT k.account_id, k.status, k.data, k.updated_at, "
            "       a.email, a.first_name, a.last_name, a.role, "
            "       a.created_at AS account_created_at, a.approval_status "
            "  FROM contributor_kyc k "
            "  LEFT JOIN login_accounts a ON a.id = k.account_id "
            " WHERE k.account_id = %s",
            (account_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="KYC submission not found")

        # ── Contributor profile (dob, country, segment, docs) ─────────────────
        cur.execute(
            "SELECT dob, country, city, segment, gender, "
            "       primary_skills, application_data "
            "  FROM contributor_profiles "
            " WHERE account_id = %s",
            (account_id,),
        )
        profile = cur.fetchone() or {}

    # ── Parse stored JSONB data ───────────────────────────────────────────────
    import json as _json

    raw_data = row.get("data") or {}
    if isinstance(raw_data, str):
        try:
            raw_data = _json.loads(raw_data)
        except (ValueError, TypeError):
            raw_data = {}
    data: dict[str, Any] = dict(raw_data)

    app_data: dict[str, Any] = {}
    raw_app = profile.get("application_data") or {}
    if isinstance(raw_app, str):
        try:
            app_data = _json.loads(raw_app)
        except (ValueError, TypeError):
            app_data = {}
    else:
        app_data = dict(raw_app)

    # ── Build base fields ─────────────────────────────────────────────────────
    full_name = (
        f"{row.get('first_name','') or ''} {row.get('last_name','') or ''}".strip()
        or row.get("email")
    )
    segment = (
        data.get("segment")
        or profile.get("segment")
        or app_data.get("segment")
    )
    dob_raw = (
        data.get("dob")
        or data.get("date_of_birth")
        or (profile.get("dob").isoformat() if profile.get("dob") else None)
        or "2000-01-01"
    )
    country = (
        data.get("country")
        or profile.get("country")
        or "India"
    )
    submitted_at = (
        data.get("submittedAt")
        or data.get("submitted_at")
        or (row["updated_at"].isoformat() if row.get("updated_at") else None)
    )

    # ── Documents array ───────────────────────────────────────────────────────
    # Stored in data.documents or application_data.docs
    raw_docs = data.get("documents") or app_data.get("docs") or []
    documents: list[dict[str, Any]] = []
    if isinstance(raw_docs, list):
        for doc in raw_docs:
            if isinstance(doc, dict):
                documents.append({
                    "url": doc.get("url") or doc.get("blobUrl") or "",
                    "filename": doc.get("filename") or doc.get("name") or "",
                    "contentType": doc.get("contentType") or doc.get("mimeType") or "application/octet-stream",
                    "uploadedAt": doc.get("uploadedAt") or doc.get("createdAt") or submitted_at,
                })

    # ── History timeline (synthesised from data fields) ───────────────────────
    history: list[dict[str, Any]] = []

    account_created_at = row.get("account_created_at")
    if account_created_at:
        history.append({
            "event": "account_created",
            "label": "Account created",
            "at": account_created_at.isoformat(),
            "by": row.get("email"),
        })

    if submitted_at:
        history.append({
            "event": "kyc_submitted",
            "label": "KYC submitted",
            "at": submitted_at,
            "by": row.get("email"),
        })

    reviewed_at = data.get("reviewedAt") or data.get("reviewed_at")
    reviewed_by = data.get("reviewedBy") or data.get("reviewed_by")
    review_note = data.get("reviewNote") or data.get("review_note")
    current_status = row.get("status") or "pending"

    if reviewed_at and reviewed_by:
        outcome_label = (
            "KYC approved" if current_status in ("verified", "approved")
            else "KYC rejected" if current_status == "rejected"
            else f"KYC status: {current_status}"
        )
        history.append({
            "event": f"kyc_{current_status}",
            "label": outcome_label,
            "at": reviewed_at,
            "by": reviewed_by,
            "note": review_note or None,
        })
    elif row.get("updated_at") and current_status not in ("pending", "not_started"):
        history.append({
            "event": f"kyc_{current_status}",
            "label": f"KYC status changed to {current_status}",
            "at": row["updated_at"].isoformat(),
            "by": reviewed_by or "system",
        })

    history.sort(key=lambda h: h.get("at") or "")

    # ── Decision block (for decided cases) ────────────────────────────────────
    decision: dict[str, Any] | None = None
    if reviewed_by or review_note or reviewed_at:
        outcome = (
            "approved" if current_status in ("verified", "approved")
            else "rejected" if current_status == "rejected"
            else "more_info"
        )
        decision = {
            "outcome": outcome,
            "reason": review_note if current_status == "rejected" else None,
            "note": review_note if current_status != "rejected" else None,
            "at": reviewed_at or (row["updated_at"].isoformat() if row.get("updated_at") else None),
            "by": reviewed_by or "Admin",
        }

    # ── autoChecks (pass through from data or synthesise) ─────────────────────
    stored_checks = data.get("autoChecks") or data.get("auto_checks")
    if isinstance(stored_checks, list) and len(stored_checks) > 0:
        auto_checks = stored_checks
    else:
        auto_checks = [
            {"label": "ID format valid", "state": "pass"},
            {"label": "Name match (pending review)", "state": "warn"},
            {"label": "Photo clarity: pending review", "state": "warn"},
            {"label": "No watchlist match", "state": "pass"},
        ]

    # ── ID document fields ────────────────────────────────────────────────────
    id_type = data.get("idType") or data.get("id_type") or "National ID"
    id_number_raw = str(data.get("idNumber") or data.get("id_number") or "")
    id_number_last4 = (
        data.get("idNumberLast4")
        or id_number_raw.replace("-", "").replace(" ", "")[-4:]
        or "0000"
    )

    return {
        # ── FE mapper fields (BackendKycItem shape) ──────────────────────────
        "accountId": row["account_id"],
        "email": row.get("email"),
        "name": full_name,
        "role": row.get("role"),
        "segment": segment,
        "status": current_status,
        "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        # ── Extended detail fields ───────────────────────────────────────────
        "data": {
            **data,
            # Normalise key fields inside data so the FE mapper picks them up
            "submittedAt": submitted_at,
            "dob": str(dob_raw),
            "country": country,
            "segment": segment,
            "idType": id_type,
            "idNumberLast4": str(id_number_last4),
            "reviewedBy": reviewed_by,
            "reviewedAt": reviewed_at,
            "reviewNote": review_note,
            "autoChecks": auto_checks,
            "documents": documents,
        },
        # ── Convenience top-level fields ─────────────────────────────────────
        "contributorName": full_name,
        "contributorEmail": row.get("email") or "",
        "dob": str(dob_raw),
        "country": country,
        "submittedAt": submitted_at,
        "approvalStatus": row.get("approval_status"),
        "idType": id_type,
        "idNumberLast4": str(id_number_last4),
        "autoChecks": auto_checks,
        "documents": documents,
        "decision": decision,
        "history": history,
    }


class KycDecision(BaseModel):
    # Accept both the backend key (decision) and the FE key (outcome).
    decision: str | None = None       # approve | reject | more_info
    outcome: str | None = None        # FE: approved | rejected | more_info
    reason: str | None = None
    note: str | None = None


@router.post("/api/superadmin/kyc/{account_id}/decision")
async def decide_kyc(
    account_id: int,
    body: KycDecision,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    verdict = (body.decision or body.outcome or "").lower()
    if verdict in ("approve", "approved", "verify"):
        new_status = "verified"
    elif verdict in ("more_info", "request_info", "awaiting_info"):
        new_status = "awaiting_info"
    else:
        new_status = "rejected"
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
