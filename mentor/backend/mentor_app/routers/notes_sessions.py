"""
Mentor notes + sessions router — /api/mentor/notes, /api/mentor/sessions,
and /api/mentor/contributors/:contributorId/notes

Implements the FE contract from:
  /api/mentor/notes           POST
  /api/mentor/notes/:noteId   PATCH, DELETE
  /api/mentor/sessions        GET, POST
  /api/mentor/sessions/:id    GET, POST (action: held|no_show|cancel|reschedule)
  /api/mentor/contributors/:contributorId/notes  GET
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg2.extras import RealDictCursor

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import require_roles

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mentor", tags=["mentor-notes-sessions"])

# ── Auth guards ───────────────────────────────────────────────────────────────

_mentor_guard = require_roles("mentor", "admin", "superadmin", "super_admin")
MentorDep = Annotated[dict, Depends(_mentor_guard)]

_mentor_or_reviewer = require_roles("mentor", "reviewer", "admin", "superadmin", "super_admin")
MentorOrReviewerDep = Annotated[dict, Depends(_mentor_or_reviewer)]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def _serialize(row: dict) -> dict:
    out = dict(row)
    for k, v in out.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


# ── Schemas ───────────────────────────────────────────────────────────────────

class NoteCreateRequest(BaseModel):
    sessionId: Optional[str] = None
    contributorId: Optional[str] = None
    body: str = Field(min_length=1, max_length=10_000)
    visibility: str = "private"  # private | shared | public
    tenantId: Optional[str] = None


class NotePatchRequest(BaseModel):
    body: Optional[str] = Field(default=None, min_length=1, max_length=10_000)
    visibility: Optional[str] = None


class SessionCreateRequest(BaseModel):
    contributorId: str
    tenantId: Optional[str] = None
    scheduledAt: str  # ISO datetime
    durationMinutes: Optional[int] = 30
    agenda: Optional[str] = None
    meetingLink: Optional[str] = None
    timezone: Optional[str] = None


class SessionActionRequest(BaseModel):
    action: str  # held | no_show | cancel | reschedule
    reason: Optional[str] = None
    # For reschedule:
    scheduledAt: Optional[str] = None
    durationMinutes: Optional[int] = None


# ── Notes endpoints ───────────────────────────────────────────────────────────

@router.post("/notes", status_code=201)
def create_note(body: NoteCreateRequest, user: MentorDep):
    """POST /api/mentor/notes → write a coaching note."""
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO mentor_notes
                (mentor_id, tenant_id, session_id, contributor_id, visibility, body,
                 attachments, kind)
            VALUES (%s, %s, %s, %s, %s, %s, '[]'::jsonb, 'coaching')
            RETURNING id, mentor_id, tenant_id, session_id, contributor_id,
                      visibility, body, kind, created_at
            """,
            (
                mentor_id,
                body.tenantId or user.get("tenant_id"),
                body.sessionId,
                body.contributorId,
                body.visibility,
                body.body,
            ),
        )
        note = cur.fetchone()
    conn.commit()

    write_audit(
        actor_id=mentor_id,
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="mentorship.note.write",
        target="mentor_notes",
        target_id=str(note["id"]),
        service="mentor-service",
        tenant_id=body.tenantId or user.get("tenant_id"),
        extra={
            "sessionId": body.sessionId,
            "contributorId": body.contributorId,
            "visibility": body.visibility,
        },
    )
    return {"note": _serialize(note)}


@router.patch("/notes/{note_id}")
def patch_note(note_id: str, body: NotePatchRequest, user: MentorDep):
    """PATCH /api/mentor/notes/:noteId → edit (author only)."""
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM mentor_notes WHERE id = %s AND deleted_at IS NULL",
            (note_id,),
        )
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Note not found")
        if str(existing["mentor_id"]) != mentor_id:
            raise HTTPException(status_code=403, detail="Forbidden: not your note")

        sets: list[str] = []
        params: list[Any] = []
        if body.body is not None:
            sets.append("body = %s")
            params.append(body.body)
        if body.visibility is not None:
            sets.append("visibility = %s")
            params.append(body.visibility)

        if not sets:
            return {"note": _serialize(existing)}

        params.append(note_id)
        cur.execute(
            f"UPDATE mentor_notes SET {', '.join(sets)} WHERE id = %s "
            "RETURNING id, mentor_id, tenant_id, session_id, contributor_id, "
            "visibility, body, kind, created_at",
            params,
        )
        updated = cur.fetchone()
    conn.commit()

    write_audit(
        actor_id=mentor_id,
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="mentorship.note.update",
        target="mentor_notes",
        target_id=str(note_id),
        service="mentor-service",
        tenant_id=existing.get("tenant_id"),
        extra={"changed": [k for k in ["body", "visibility"] if getattr(body, k) is not None]},
    )
    return {"note": _serialize(updated)}


@router.delete("/notes/{note_id}")
def delete_note(note_id: str, user: MentorDep):
    """DELETE /api/mentor/notes/:noteId → soft delete (author only)."""
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, mentor_id, tenant_id FROM mentor_notes "
            "WHERE id = %s AND deleted_at IS NULL",
            (note_id,),
        )
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Note not found")
        if str(existing["mentor_id"]) != mentor_id:
            raise HTTPException(status_code=403, detail="Forbidden: not your note")
        cur.execute(
            "UPDATE mentor_notes SET deleted_at = now() WHERE id = %s",
            (note_id,),
        )
    conn.commit()

    write_audit(
        actor_id=mentor_id,
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="mentorship.note.delete",
        target="mentor_notes",
        target_id=str(note_id),
        service="mentor-service",
        tenant_id=existing.get("tenant_id"),
    )
    return {"deleted": True}


@router.get("/contributors/{contributor_id}/notes")
def list_contributor_notes(contributor_id: str, user: MentorOrReviewerDep):
    """GET /api/mentor/contributors/:contributorId/notes
    Caller sees shared+public notes (any mentor) + their own private notes.
    """
    caller_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, mentor_id, tenant_id, session_id, contributor_id,
                   visibility, body, kind, created_at
            FROM mentor_notes
            WHERE contributor_id = %s
              AND deleted_at IS NULL
              AND (
                visibility IN ('shared', 'public')
                OR (visibility = 'private' AND mentor_id = %s)
              )
            ORDER BY created_at DESC
            """,
            (contributor_id, caller_id),
        )
        items = [_serialize(r) for r in cur.fetchall()]
    return {"items": items}


# ── Sessions endpoints ────────────────────────────────────────────────────────

def _enrich_session(row: dict) -> dict:
    """Add human-readable fields where possible (contributor name lookup skipped
    for performance; FE can join from its own context)."""
    return _serialize(row)


@router.get("/sessions")
def list_sessions(
    user: MentorDep,
    status: Optional[list[str]] = Query(default=None),
    upcomingOnly: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
):
    """GET /api/mentor/sessions — list mentor's sessions."""
    mentor_id = str(user["id"])
    where = ["mentor_id = %s"]
    params: list[Any] = [mentor_id]

    if status:
        placeholders = ", ".join(["%s"] * len(status))
        where.append(f"status IN ({placeholders})")
        params.extend(status)

    if upcomingOnly:
        where.append("scheduled_at > now()")

    clause = " AND ".join(where)
    params.append(limit)

    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT id, mentor_id, contributor_id, tenant_id, scheduled_at,
                   duration_minutes, agenda, meeting_link, timezone, status,
                   cancel_reason, created_by, created_at, updated_at
            FROM mentor_sessions
            WHERE {clause}
            ORDER BY scheduled_at DESC
            LIMIT %s
            """,
            params,
        )
        items = [_enrich_session(r) for r in cur.fetchall()]
    return {"items": items, "total": len(items)}


@router.post("/sessions", status_code=201)
def create_session(body: SessionCreateRequest, user: MentorDep):
    """POST /api/mentor/sessions — schedule a new session."""
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO mentor_sessions
                (mentor_id, contributor_id, tenant_id, scheduled_at,
                 duration_minutes, agenda, meeting_link, timezone, created_by)
            VALUES (%s, %s, %s, %s::TIMESTAMPTZ, %s, %s, %s, %s, %s)
            RETURNING id, mentor_id, contributor_id, tenant_id, scheduled_at,
                      duration_minutes, agenda, meeting_link, timezone, status,
                      cancel_reason, created_by, created_at, updated_at
            """,
            (
                mentor_id,
                body.contributorId,
                body.tenantId or user.get("tenant_id"),
                body.scheduledAt,
                body.durationMinutes or 30,
                body.agenda,
                body.meetingLink,
                body.timezone,
                mentor_id,
            ),
        )
        session = cur.fetchone()
    conn.commit()

    write_audit(
        actor_id=mentor_id,
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="mentorship.session.schedule",
        target="mentor_sessions",
        target_id=str(session["id"]),
        service="mentor-service",
        tenant_id=body.tenantId or user.get("tenant_id"),
        extra={
            "contributorId": body.contributorId,
            "scheduledAt": body.scheduledAt,
            "durationMinutes": body.durationMinutes,
        },
    )
    return {"session": _enrich_session(session)}


@router.get("/sessions/{session_id}")
def get_session(session_id: str, user: MentorDep):
    """GET /api/mentor/sessions/:sessionId — detail."""
    mentor_id = str(user["id"])
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, mentor_id, contributor_id, tenant_id, scheduled_at,
                   duration_minutes, agenda, meeting_link, timezone, status,
                   cancel_reason, created_by, created_at, updated_at
            FROM mentor_sessions WHERE id = %s
            """,
            (session_id,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Session not found")

    # Enforce ownership (unless admin)
    role = (user.get("role") or "").lower()
    if str(row["mentor_id"]) != mentor_id and role not in {"admin", "superadmin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Forbidden")

    return {"session": _enrich_session(row)}


@router.post("/sessions/{session_id}")
def session_action(session_id: str, body: SessionActionRequest, user: MentorDep):
    """POST /api/mentor/sessions/:sessionId — action: held | no_show | cancel | reschedule."""
    valid_actions = {"held", "no_show", "cancel", "reschedule"}
    if body.action not in valid_actions:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action. Must be one of: {', '.join(sorted(valid_actions))}",
        )

    mentor_id = str(user["id"])
    conn = _conn()

    # Status machine
    _TERMINAL = {"held", "no_show", "cancelled"}
    _ACTION_STATUS = {
        "held": "held",
        "no_show": "no_show",
        "cancel": "cancelled",
        "reschedule": "rescheduled",
    }

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM mentor_sessions WHERE id = %s",
            (session_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")

        role = (user.get("role") or "").lower()
        if str(row["mentor_id"]) != mentor_id and role not in {"admin", "superadmin", "super_admin"}:
            raise HTTPException(status_code=403, detail="Forbidden")

        if row["status"] in _TERMINAL:
            raise HTTPException(
                status_code=400,
                detail=f"Session is already in terminal state '{row['status']}'",
            )

        new_status = _ACTION_STATUS[body.action]
        sets = ["status = %s", "updated_at = now()"]
        params: list[Any] = [new_status]

        if body.action == "cancel" and body.reason:
            sets.append("cancel_reason = %s")
            params.append(body.reason)

        if body.action == "reschedule":
            if body.scheduledAt:
                sets.append("scheduled_at = %s::TIMESTAMPTZ")
                params.append(body.scheduledAt)
            if body.durationMinutes is not None:
                sets.append("duration_minutes = %s")
                params.append(body.durationMinutes)

        params.append(session_id)
        cur.execute(
            f"UPDATE mentor_sessions SET {', '.join(sets)} WHERE id = %s "
            "RETURNING id, mentor_id, contributor_id, tenant_id, scheduled_at, "
            "duration_minutes, agenda, meeting_link, timezone, status, "
            "cancel_reason, created_by, created_at, updated_at",
            params,
        )
        updated = cur.fetchone()
    conn.commit()

    severity = "warning" if body.action == "cancel" else "info"
    write_audit(
        actor_id=mentor_id,
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action=f"mentorship.session.{body.action}",
        target="mentor_sessions",
        target_id=str(session_id),
        service="mentor-service",
        tenant_id=row.get("tenant_id"),
        extra={"reason": body.reason or None, "severity": severity},
    )
    return {"session": _enrich_session(updated)}
