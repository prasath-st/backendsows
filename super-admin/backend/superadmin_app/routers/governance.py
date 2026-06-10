"""
Governance / Trust-&-Safety case queue for the Super Admin portal.

Spec: MockGovCase in mocks/admin/governance.ts + lib/admin/mocks/governance-service.ts

Endpoints
---------
GET  /api/superadmin/governance                     — list (filter ?type&status&severity)
GET  /api/superadmin/governance/{case_id}           — single case detail
PATCH /api/superadmin/governance/{case_id}/assign   — body {assignedTo: str}
POST /api/superadmin/governance/{case_id}/notes     — body {text: str}
POST /api/superadmin/governance/{case_id}/actions   — body {action: str}
POST /api/superadmin/governance/{case_id}/close     — body {decision, summary, actions?}

Table: governance_cases  (snake_case; JSONB for nested fields)
Schema is created idempotently on import; seed data inserted on first run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Any

import psycopg2
from fastapi import APIRouter, Body, Depends, HTTPException
from psycopg2.extras import Json, RealDictCursor
from pydantic import BaseModel

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["superadmin-governance"])

# ── helpers ────────────────────────────────────────────────────────────────────

def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _load_json(val: Any) -> Any:
    """Safely parse a JSONB column that may already be a dict/list or a raw string."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (TypeError, ValueError):
        return val


def _row_out(r: dict) -> dict[str, Any]:
    """Map a governance_cases DB row → MockGovCase camelCase shape."""
    return {
        "id": r["id"],
        "type": r["type"],
        "severity": r["severity"],
        "source": r["source"],
        "anonymous": r["anonymous"],
        "openedAt": _iso(r.get("opened_at")),
        "assignedTo": r.get("assigned_to"),
        "status": r["status"],
        "report": _load_json(r.get("report")) or {},
        "context": _load_json(r.get("context")) or {"contributorIdentityRedacted": False},
        "internalNotes": _load_json(r.get("internal_notes")) or [],
        "actionsTaken": _load_json(r.get("actions_taken")) or [],
        "resolution": _load_json(r.get("resolution")),
    }


# ── schema init + seed ─────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS governance_cases (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    source          TEXT NOT NULL,
    anonymous       BOOLEAN NOT NULL DEFAULT FALSE,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    assigned_to     TEXT,
    status          TEXT NOT NULL DEFAULT 'open',
    report          JSONB NOT NULL DEFAULT '{}',
    context         JSONB NOT NULL DEFAULT '{}',
    internal_notes  JSONB NOT NULL DEFAULT '[]',
    actions_taken   JSONB NOT NULL DEFAULT '[]',
    resolution      JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# Seed rows derived from MOCK_GOV_CASES in governance.ts
_SEED_ROWS: list[dict[str, Any]] = [
    {
        "id": "GR-1042",
        "type": "safety_report",
        "severity": "high",
        "source": "contributor",
        "anonymous": True,
        "opened_at": "2026-05-27T06:00:00Z",
        "assigned_to": "Sneha Pillai",
        "status": "open",
        "report": {
            "category": "Harassment",
            "incidentDate": "2026-05-25",
            "description": "During a mentorship session, the mentor made inappropriate comments about my appearance. I was uncomfortable for the rest of the session.",
        },
        "context": {
            "relatedSessionId": "ms-2092",
            "sessionAt": "2026-05-25T14:00:00+05:30",
            "sessionDurationMin": 30,
            "mentorId": "m-rajesh",
            "mentorName": "Rajesh Verma",
            "contributorIdentityRedacted": True,
        },
        "internal_notes": [],
        "actions_taken": [],
        "resolution": None,
    },
    {
        "id": "GR-1041",
        "type": "dispute",
        "severity": "medium",
        "source": "contributor",
        "anonymous": False,
        "opened_at": "2026-05-27T03:00:00Z",
        "assigned_to": "Sneha Pillai",
        "status": "in_review",
        "report": {
            "category": "Mentor decision dispute",
            "description": "Mentor rejected my submission citing insufficient tests but tests were included as instructed. Asking for re-review by a different mentor.",
        },
        "context": {
            "relatedSessionId": "rv-8801",
            "mentorId": "m-fatima",
            "mentorName": "Fatima Nair",
            "enterpriseId": "t-acme",
            "enterpriseName": "Acme Corp",
            "contributorIdentityRedacted": False,
        },
        "internal_notes": [
            {
                "at": "2026-05-27T04:30:00Z",
                "by": "Sneha Pillai",
                "text": "Pulled both submission versions. Tests present in v2; mentor may have missed them. Asking for second-pair review.",
            }
        ],
        "actions_taken": [],
        "resolution": None,
    },
    {
        "id": "GR-1040",
        "type": "mentor_escalation",
        "severity": "medium",
        "source": "mentor",
        "anonymous": False,
        "opened_at": "2026-05-27T01:00:00Z",
        "assigned_to": None,
        "status": "open",
        "report": {
            "category": "Repeated low-quality submissions",
            "description": "Contributor has missed acceptance criteria across three consecutive tasks. Escalating to determine fit + corrective coaching plan.",
        },
        "context": {
            "mentorId": "m-priya",
            "mentorName": "Priya Iyer",
            "enterpriseId": "t-acme",
            "enterpriseName": "Acme Corp",
            "contributorIdentityRedacted": False,
        },
        "internal_notes": [],
        "actions_taken": [],
        "resolution": None,
    },
    {
        "id": "GR-1039",
        "type": "grievance",
        "severity": "low",
        "source": "contributor",
        "anonymous": False,
        "opened_at": "2026-05-26T08:00:00Z",
        "assigned_to": "Sneha Pillai",
        "status": "resolved_no_action",
        "report": {
            "category": "Payout delay",
            "description": "Withdrawal of ₹4,200 has been pending for 5 days. No response on support ticket.",
        },
        "context": {
            "contributorIdentityRedacted": False,
        },
        "internal_notes": [
            {
                "at": "2026-05-26T09:00:00Z",
                "by": "Sneha Pillai",
                "text": "Coordinated with Payments. Razorpay UPI was degraded; payout reattempted and succeeded.",
            }
        ],
        "actions_taken": [],
        "resolution": {
            "decision": "resolved_no_action",
            "summary": "Payout completed after rail recovery; contributor notified.",
            "actions": [],
            "at": "2026-05-26T10:00:00Z",
            "by": "Sneha Pillai",
        },
    },
    {
        "id": "GR-1038",
        "type": "safety_report",
        "severity": "high",
        "source": "contributor",
        "anonymous": False,
        "opened_at": "2026-05-22T11:00:00Z",
        "assigned_to": "Sneha Pillai",
        "status": "escalated",
        "report": {
            "category": "Discriminatory language",
            "description": "Mentor used disparaging language in a written review. I have a screenshot.",
        },
        "context": {
            "relatedSessionId": "rv-8773",
            "mentorId": "m-marco",
            "mentorName": "Marco Bianchi",
            "enterpriseId": "t-helios",
            "enterpriseName": "Helios Studios",
            "contributorIdentityRedacted": False,
        },
        "internal_notes": [
            {
                "at": "2026-05-22T13:00:00Z",
                "by": "Sneha Pillai",
                "text": "Evidence reviewed. Pattern matches one prior complaint (closed; unsubstantiated). Forwarding to legal for next steps.",
            }
        ],
        "actions_taken": [],
        "resolution": None,
    },
    {
        "id": "GR-1037",
        "type": "grievance",
        "severity": "medium",
        "source": "enterprise",
        "anonymous": False,
        "opened_at": "2026-05-28T02:00:00Z",
        "assigned_to": None,
        "status": "open",
        "report": {
            "category": "Reviewer bias concern",
            "description": "Enterprise reviewer consistently rejects submissions from women contributors on our project. Requesting platform review of review patterns.",
        },
        "context": {
            "enterpriseId": "t-acme",
            "enterpriseName": "Acme Corp",
            "contributorIdentityRedacted": False,
        },
        "internal_notes": [],
        "actions_taken": [],
        "resolution": None,
    },
    {
        "id": "GR-1036",
        "type": "dispute",
        "severity": "low",
        "source": "contributor",
        "anonymous": False,
        "opened_at": "2026-05-27T18:00:00Z",
        "assigned_to": "Sneha Pillai",
        "status": "pending_legal",
        "report": {
            "category": "IP attribution dispute",
            "description": "Contributor claims prior art was used without credit in accepted deliverable. Seeking legal guidance on attribution policy.",
        },
        "context": {
            "enterpriseId": "t-helios",
            "enterpriseName": "Helios Studios",
            "contributorIdentityRedacted": False,
        },
        "internal_notes": [
            {
                "at": "2026-05-27T19:00:00Z",
                "by": "Sneha Pillai",
                "text": "Preliminary review complete. Escalating to legal for IP policy interpretation.",
            }
        ],
        "actions_taken": [],
        "resolution": None,
    },
    {
        "id": "GR-1035",
        "type": "safety_report",
        "severity": "high",
        "source": "contributor",
        "anonymous": True,
        "opened_at": "2026-05-28T04:30:00Z",
        "assigned_to": None,
        "status": "open",
        "report": {
            "category": "Threatening language",
            "incidentDate": "2026-05-27",
            "description": "Received threatening messages in platform chat after declining extra unpaid work. Screenshot attached in ticket.",
        },
        "context": {
            "relatedSessionId": "ms-2101",
            "sessionAt": "2026-05-27T16:00:00+05:30",
            "mentorId": "m-alex",
            "mentorName": "Alex Chen",
            "contributorIdentityRedacted": True,
        },
        "internal_notes": [],
        "actions_taken": [],
        "resolution": None,
    },
]


def init_governance_schema() -> None:
    """Create governance_cases table if absent and seed mock data."""
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(_DDL)
            conn.commit()

        # Seed rows — INSERT ... ON CONFLICT DO NOTHING (idempotent)
        with conn.cursor() as cur:
            for row in _SEED_ROWS:
                cur.execute(
                    """
                    INSERT INTO governance_cases
                        (id, type, severity, source, anonymous, opened_at, assigned_to,
                         status, report, context, internal_notes, actions_taken, resolution)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        row["id"],
                        row["type"],
                        row["severity"],
                        row["source"],
                        row["anonymous"],
                        row["opened_at"],
                        row.get("assigned_to"),
                        row["status"],
                        Json(row["report"]),
                        Json(row["context"]),
                        Json(row["internal_notes"]),
                        Json(row["actions_taken"]),
                        Json(row["resolution"]) if row.get("resolution") else None,
                    ),
                )
        conn.commit()
        logger.info("governance_cases table ready.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("governance schema init failed (non-fatal): %s", exc)
        try:
            _conn().rollback()
        except Exception:
            pass


# Run on import so the table exists before the first request.
init_governance_schema()


# ── Pydantic request bodies ────────────────────────────────────────────────────

class _AssignBody(BaseModel):
    assignedTo: str


class _NoteBody(BaseModel):
    text: str


class _ActionBody(BaseModel):
    action: str


class _CloseBody(BaseModel):
    decision: str   # resolved_action | resolved_no_action | escalated
    summary: str
    actions: list[str] = []


# ── GET /api/superadmin/governance ─────────────────────────────────────────────

@router.get("/api/superadmin/governance")
async def list_governance_cases(
    admin: Annotated[dict, Depends(get_current_admin)],
    type: str | None = None,       # noqa: A002
    status: str | None = None,
    severity: str | None = None,
):
    """Return all governance cases, newest first.

    Supports optional query-string filters: type, status, severity.
    Returns a flat array (matching the FE listAdminGovCases() shape).
    """
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        conditions: list[str] = []
        params: list[Any] = []
        if type:
            conditions.append("type = %s")
            params.append(type)
        if status:
            conditions.append("status = %s")
            params.append(status)
        if severity:
            conditions.append("severity = %s")
            params.append(severity)
        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        cur.execute(
            f"SELECT * FROM governance_cases {where_clause} ORDER BY opened_at DESC",
            params,
        )
        rows = cur.fetchall()
    return [_row_out(dict(r)) for r in rows]


# ── GET /api/superadmin/governance/{case_id} ───────────────────────────────────

@router.get("/api/superadmin/governance/{case_id}")
async def get_governance_case(
    case_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Return a single governance case by ID."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM governance_cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "case_not_found", "caseId": case_id})
    return _row_out(dict(row))


# ── PATCH /api/superadmin/governance/{case_id}/assign ──────────────────────────

@router.patch("/api/superadmin/governance/{case_id}/assign")
async def assign_governance_case(
    case_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _AssignBody = Body(...),
):
    """Assign (or reassign) a case to a T&S operator by name.

    If the case is currently 'open' it is advanced to 'in_review' automatically,
    matching the FE takeGovCase() / reassignGovCase() behaviour.
    """
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM governance_cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "case_not_found", "caseId": case_id})

    row = dict(row)
    _check_not_locked(row, case_id)

    new_status = "in_review" if row["status"] == "open" else row["status"]

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE governance_cases
               SET assigned_to = %s,
                   status = %s,
                   updated_at = NOW()
             WHERE id = %s
            RETURNING *
            """,
            (body.assignedTo, new_status, case_id),
        )
        updated = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"),
            actor_email=admin.get("email"),
            actor_role=admin.get("role"),
            action="governance_assign",
            target=case_id,
            target_id=case_id,
            details=f"assigned_to={body.assignedTo}",
        )
    except Exception:  # noqa: BLE001
        pass

    return _row_out(dict(updated))


# ── POST /api/superadmin/governance/{case_id}/notes ────────────────────────────

@router.post("/api/superadmin/governance/{case_id}/notes")
async def add_governance_note(
    case_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _NoteBody = Body(...),
):
    """Append an internal note to the case (actor name from token email)."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM governance_cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "case_not_found", "caseId": case_id})

    row = dict(row)
    _check_not_locked(row, case_id)

    existing_notes: list[dict] = _load_json(row.get("internal_notes")) or []
    by_name = admin.get("email") or "admin"
    new_note = {
        "at": datetime.now(timezone.utc).isoformat(),
        "by": by_name,
        "text": body.text.strip(),
    }
    updated_notes = existing_notes + [new_note]

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE governance_cases
               SET internal_notes = %s,
                   updated_at = NOW()
             WHERE id = %s
            RETURNING *
            """,
            (Json(updated_notes), case_id),
        )
        updated = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"),
            actor_email=admin.get("email"),
            actor_role=admin.get("role"),
            action="governance_add_note",
            target=case_id,
            target_id=case_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return _row_out(dict(updated))


# ── POST /api/superadmin/governance/{case_id}/actions ─────────────────────────

@router.post("/api/superadmin/governance/{case_id}/actions")
async def apply_governance_action(
    case_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _ActionBody = Body(...),
):
    """Log an investigation action; may also side-effect the status.

    Mirrors FE applyGovCaseAction():
    - "Forward to legal"  → status becomes pending_legal
    - "Suspend mentor"    → status stays in_review
    - Any other action    → no status change
    """
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM governance_cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "case_not_found", "caseId": case_id})

    row = dict(row)
    _check_not_locked(row, case_id)

    existing_actions: list[str] = _load_json(row.get("actions_taken")) or []
    actor_name = admin.get("email") or "admin"
    stamp = datetime.now(timezone.utc).strftime("%d/%m/%Y, %H:%M:%S")
    action_entry = f"{body.action} · {actor_name} · {stamp}"
    updated_actions = existing_actions + [action_entry]

    new_status = row["status"]
    if body.action == "Forward to legal":
        new_status = "pending_legal"

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE governance_cases
               SET actions_taken = %s,
                   status = %s,
                   updated_at = NOW()
             WHERE id = %s
            RETURNING *
            """,
            (Json(updated_actions), new_status, case_id),
        )
        updated = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"),
            actor_email=admin.get("email"),
            actor_role=admin.get("role"),
            action="governance_action",
            target=case_id,
            target_id=case_id,
            details=body.action,
        )
    except Exception:  # noqa: BLE001
        pass

    return _row_out(dict(updated))


# ── POST /api/superadmin/governance/{case_id}/close ───────────────────────────

@router.post("/api/superadmin/governance/{case_id}/close")
async def close_governance_case(
    case_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _CloseBody = Body(...),
):
    """Close a case with a decision + summary + optional extra actions list.

    Valid decisions: resolved_action | resolved_no_action | escalated
    Mirrors FE closeGovCase(); carries over any earlier actionsTaken.
    """
    _VALID_DECISIONS = {"resolved_action", "resolved_no_action", "escalated"}
    if body.decision not in _VALID_DECISIONS:
        raise HTTPException(
            status_code=422,
            detail=f"decision must be one of {sorted(_VALID_DECISIONS)}",
        )

    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM governance_cases WHERE id = %s", (case_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "case_not_found", "caseId": case_id})

    row = dict(row)
    _check_not_locked(row, case_id)

    existing_actions: list[str] = _load_json(row.get("actions_taken")) or []
    # Merge caller-supplied actions_override list with existing log entries
    merged_actions = existing_actions + [a for a in body.actions if a not in existing_actions]

    actor_name = admin.get("email") or "admin"
    now_iso = datetime.now(timezone.utc).isoformat()

    resolution = {
        "decision": body.decision,
        "summary": body.summary.strip(),
        "actions": merged_actions,
        "at": now_iso,
        "by": actor_name,
    }

    final_status: str = body.decision  # decision string equals GovCaseStatus value

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE governance_cases
               SET status = %s,
                   actions_taken = %s,
                   resolution = %s,
                   updated_at = NOW()
             WHERE id = %s
            RETURNING *
            """,
            (final_status, Json(merged_actions), Json(resolution), case_id),
        )
        updated = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"),
            actor_email=admin.get("email"),
            actor_role=admin.get("role"),
            action="governance_close",
            target=case_id,
            target_id=case_id,
            details=f"decision={body.decision}",
        )
    except Exception:  # noqa: BLE001
        pass

    return _row_out(dict(updated))


# ── guard helper ───────────────────────────────────────────────────────────────

_LOCKED_STATUSES = {"escalated", "pending_legal", "resolved_action", "resolved_no_action"}


def _check_not_locked(row: dict, case_id: str) -> None:
    """Raise 409 if the case is in a terminal / locked status."""
    if row.get("status") in _LOCKED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "case_locked",
                "caseId": case_id,
                "status": row["status"],
                "message": "Case is in a locked/terminal status and cannot be mutated.",
            },
        )
