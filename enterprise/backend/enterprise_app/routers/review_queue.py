"""Enterprise review-queue — /api/v1/enterprise/review-queue/**

Surfaces mentor-accepted submissions that are awaiting a final enterprise
decision.  Uses the dedicated ``enterprise_review_queue`` Postgres table so
state (claim / decision) is durable and isolated from the main deliverables
table.

Routes:
  GET  /api/v1/enterprise/review-queue          list pending + optionally claimed
  GET  /api/v1/enterprise/review-queue/history  decided (accepted/rework) items
  GET  /api/v1/enterprise/review-queue/{id}     single submission detail
  POST /api/v1/enterprise/review-queue/{id}/claim
  POST /api/v1/enterprise/review-queue/{id}/release
  POST /api/v1/enterprise/review-queue/{id}/decide

Response shapes must match the FE contracts in:
  src/lib/api/enterprise-review.ts
  src/lib/enterprise-review/types.ts
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from psycopg2.extras import Json, RealDictCursor

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/enterprise/review-queue", tags=["review-queue"])

# ── DDL ─────────────────────────────────────────────────────────────────────

REVIEW_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS enterprise_review_queue (
    submission_id   TEXT PRIMARY KEY,
    data            JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'pending',
    claimed_by      TEXT,
    claimed_at      TIMESTAMPTZ,
    decision        TEXT,
    decided_at      TIMESTAMPTZ,
    decided_by      TEXT,
    decision_note   TEXT,
    decision_id     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ent_rq_status ON enterprise_review_queue(status);
CREATE INDEX IF NOT EXISTS idx_ent_rq_claimed ON enterprise_review_queue(claimed_by);
"""


def init_review_queue_schema() -> None:
    """Idempotently create the enterprise_review_queue table."""
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(REVIEW_QUEUE_DDL)
    conn.commit()


# ── helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = "") -> str:
    base = uuid.uuid4().hex[:24]
    return f"{prefix}{base}" if prefix else base


def _row_to_item(row: dict) -> dict:
    """Convert a DB row (RealDictRow) to the EnterpriseReviewQueueItem shape."""
    data: dict[str, Any] = dict(row.get("data") or {})
    # Overlay the mutable columns that can change after initial insert.
    data["enterpriseReviewerId"] = row.get("claimed_by")
    data["enterpriseReviewerAssignedAt"] = (
        row["claimed_at"].isoformat() if row.get("claimed_at") else None
    )
    return data


def _fetch_row(submission_id: str) -> dict | None:
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM enterprise_review_queue WHERE submission_id = %s",
            [submission_id],
        )
        return cur.fetchone()


# ── list ─────────────────────────────────────────────────────────────────────

@router.get("")
def list_review_queue(
    user: Annotated[dict, Depends(get_current_user)],
    mine: bool = Query(False),
    includeClaimed: bool = Query(False),
    limit: int | None = Query(None),
) -> dict:
    """Return pending (undecided) items in the enterprise review queue.

    Query params:
      mine           – only items claimed by the current user
      includeClaimed – include items claimed by *anyone*; default excludes
                       items claimed by others (except the caller)
      limit          – cap the result count
    """
    ensure_pg_clean()
    conn = get_pg_connection()
    clauses = ["decision IS NULL"]
    params: list[Any] = []

    if mine:
        clauses.append("claimed_by = %s")
        params.append(user["id"])
    elif not includeClaimed:
        # Return unclaimed OR claimed by this user.
        clauses.append("(claimed_by IS NULL OR claimed_by = %s)")
        params.append(user["id"])

    sql = (
        "SELECT * FROM enterprise_review_queue"
        + (" WHERE " + " AND ".join(clauses) if clauses else "")
        + " ORDER BY created_at ASC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    items = [_row_to_item(r) for r in rows]
    return {"items": items}


# ── history ───────────────────────────────────────────────────────────────────

@router.get("/history")
def list_review_history(
    user: Annotated[dict, Depends(get_current_user)],
    limit: int | None = Query(None),
) -> dict:
    """Return decided items (accept / rework) for the history view."""
    ensure_pg_clean()
    conn = get_pg_connection()
    sql = (
        "SELECT * FROM enterprise_review_queue"
        " WHERE decision IS NOT NULL"
        " ORDER BY decided_at DESC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    items = []
    for r in rows:
        base = _row_to_item(r)
        items.append({
            **base,
            "decision": r["decision"],
            "decidedAt": r["decided_at"].isoformat() if r.get("decided_at") else None,
            "decisionId": r.get("decision_id"),
            "note": r.get("decision_note"),
        })
    return {"items": items}


# ── detail ────────────────────────────────────────────────────────────────────

@router.get("/{submission_id}")
def get_review_submission(
    submission_id: str,
    user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Return a single submission with its current state + any decision."""
    row = _fetch_row(submission_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Submission not found")

    item = _row_to_item(row)

    decided: dict | None = None
    if row.get("decision"):
        decided = {
            **item,
            "decision": row["decision"],
            "decidedAt": row["decided_at"].isoformat() if row.get("decided_at") else None,
            "decisionId": row.get("decision_id"),
            "note": row.get("decision_note"),
        }

    return {"item": item, "decided": decided}


# ── claim ─────────────────────────────────────────────────────────────────────

@router.post("/{submission_id}/claim")
def claim_review(
    submission_id: str,
    user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Assign the current user as the enterprise reviewer for this submission."""
    row = _fetch_row(submission_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    if row.get("decision"):
        raise HTTPException(status_code=409, detail="Submission already decided")
    if row.get("claimed_by") and row["claimed_by"] != user["id"]:
        raise HTTPException(status_code=409, detail="Submission already claimed by another reviewer")

    ensure_pg_clean()
    conn = get_pg_connection()
    now = datetime.now(timezone.utc)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE enterprise_review_queue
               SET claimed_by = %s,
                   claimed_at = %s,
                   updated_at = now()
             WHERE submission_id = %s
            RETURNING *
            """,
            [user["id"], now, submission_id],
        )
        updated = cur.fetchone()
    conn.commit()

    write_audit(
        actor_id=user["id"],
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="enterprise_review_claim",
        target="enterprise_review_queue",
        target_id=submission_id,
        details=f"Claimed by {user.get('email')}",
        service="enterprise",
    )

    return {"item": _row_to_item(updated)}


# ── release ───────────────────────────────────────────────────────────────────

@router.post("/{submission_id}/release")
def release_review(
    submission_id: str,
    user: Annotated[dict, Depends(get_current_user)],
) -> dict:
    """Release a claim so another reviewer can take it."""
    row = _fetch_row(submission_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    if row.get("decision"):
        raise HTTPException(status_code=409, detail="Submission already decided — cannot release")
    # Only the claimer (or an admin) may release.
    is_admin = (user.get("role") or "").lower() in {"admin", "superadmin", "super_admin"}
    if row.get("claimed_by") and row["claimed_by"] != user["id"] and not is_admin:
        raise HTTPException(status_code=403, detail="You do not hold this claim")

    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE enterprise_review_queue
               SET claimed_by = NULL,
                   claimed_at = NULL,
                   updated_at = now()
             WHERE submission_id = %s
            RETURNING *
            """,
            [submission_id],
        )
        cur.fetchone()
    conn.commit()

    write_audit(
        actor_id=user["id"],
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="enterprise_review_release",
        target="enterprise_review_queue",
        target_id=submission_id,
        details=f"Released by {user.get('email')}",
        service="enterprise",
    )

    return {"released": True}


# ── decide ────────────────────────────────────────────────────────────────────

@router.post("/{submission_id}/decide")
def decide_review(
    submission_id: str,
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
) -> dict:
    """Record a final enterprise decision: 'accept' or 'rework'.

    Body: { decision: 'accept' | 'rework', note?: string, deciderInitials?: string }
    Returns: { result: EnterpriseDecisionResult }
    """
    row = _fetch_row(submission_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    if row.get("decision"):
        raise HTTPException(status_code=409, detail="Submission already has a decision")

    decision = (body or {}).get("decision")
    valid = {"accept", "rework"}
    if decision not in valid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid decision '{decision}'. Must be one of: {', '.join(sorted(valid))}.",
        )

    note = (body or {}).get("note") or None
    decision_id = _new_id("dec_")
    now = datetime.now(timezone.utc)

    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE enterprise_review_queue
               SET decision      = %s,
                   decided_at    = %s,
                   decided_by    = %s,
                   decision_note = %s,
                   decision_id   = %s,
                   status        = %s,
                   updated_at    = now()
             WHERE submission_id = %s
            RETURNING *
            """,
            [
                decision,
                now,
                user["id"],
                note,
                decision_id,
                "accepted" if decision == "accept" else "rework_requested",
                submission_id,
            ],
        )
        updated = cur.fetchone()
    conn.commit()

    if updated is None:
        raise HTTPException(status_code=404, detail="Submission not found after update")

    write_audit(
        actor_id=user["id"],
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="enterprise_review_decide",
        target="enterprise_review_queue",
        target_id=submission_id,
        details=f"Decision '{decision}' by {user.get('email')}. note={note!r}",
        service="enterprise",
        extra={"decision": decision, "decisionId": decision_id},
    )

    result = {
        "submissionId": submission_id,
        "decision": decision,
        "decisionId": decision_id,
        "decidedAt": now.isoformat(),
    }
    return {"result": result}
