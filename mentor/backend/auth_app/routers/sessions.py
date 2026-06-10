"""
Sessions router — /api/v1/sessions

Exposes the current user's active login sessions and allows revoking them.

FE proxy paths:
  GET    /api/sessions          → GET    /api/v1/sessions
  DELETE /api/sessions/:id      → DELETE /api/v1/sessions/:id

Auth router duplicates at /api/v1/auth/sessions + /api/v1/auth/sessions/:id
are also handled here and re-exported (see bottom of file) so both URL shapes
work for the FE helpers in auth.ts (getSessions / revokeSession).

The `auth_sessions` table (created in shared/init_schema.py) stores one row
per login event. We extend it with extra columns (idempotent ALTERs) for the
UserSessionRecord shape the FE expects.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg2.extras import RealDictCursor

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_user

logger = logging.getLogger(__name__)

# ── Routers ───────────────────────────────────────────────────────────────────
# Primary mount point matches the FE proxy (GET /api/sessions → /api/v1/sessions)
router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])

# Secondary mount under /api/v1/auth/sessions (used by authApi.getSessions)
auth_sessions_router = APIRouter(prefix="/api/v1/auth/sessions", tags=["sessions"])


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def _ensure_extra_columns() -> None:
    """Add optional columns to auth_sessions if they don't exist yet."""
    ddl = """
    ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS browser   TEXT;
    ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS os        TEXT;
    ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS city      TEXT;
    ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS country   TEXT;
    """
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


# ── Simple UA parser (no extra deps) ─────────────────────────────────────────

def _parse_ua(ua: str | None) -> dict[str, str | None]:
    if not ua:
        return {"browser": None, "os": None, "device": None}
    ua_lower = ua.lower()

    # Browser
    if "edg/" in ua_lower or "edghtml" in ua_lower:
        browser = "Edge"
    elif "opr/" in ua_lower or "opera" in ua_lower:
        browser = "Opera"
    elif "chrome/" in ua_lower and "chromium" not in ua_lower:
        browser = "Chrome"
    elif "firefox/" in ua_lower:
        browser = "Firefox"
    elif "safari/" in ua_lower and "chrome" not in ua_lower:
        browser = "Safari"
    else:
        browser = "Browser"

    # OS
    if "android" in ua_lower:
        os_ = "Android"
        # Try to extract version
        m = re.search(r"android (\d+[\.\d]*)", ua_lower)
        if m:
            os_ = f"Android {m.group(1)}"
    elif "iphone" in ua_lower or "ipad" in ua_lower:
        os_ = "iOS"
        m = re.search(r"os (\d+[_\d]*) like", ua_lower)
        if m:
            os_ = f"iOS {m.group(1).replace('_', '.')}"
    elif "windows nt" in ua_lower:
        nt_map = {
            "10.0": "Windows 10/11",
            "6.3": "Windows 8.1",
            "6.2": "Windows 8",
            "6.1": "Windows 7",
        }
        m = re.search(r"windows nt ([\d.]+)", ua_lower)
        os_ = nt_map.get(m.group(1) if m else "", "Windows") if m else "Windows"
    elif "mac os x" in ua_lower:
        os_ = "macOS"
    elif "linux" in ua_lower:
        os_ = "Linux"
    else:
        os_ = None

    # Device hint
    if any(x in ua_lower for x in ("iphone", "android", "mobile")):
        device = "Mobile"
    elif "ipad" in ua_lower or "tablet" in ua_lower:
        device = "Tablet"
    else:
        device = "Desktop"

    return {"browser": browser, "os": os_, "device": device}


# ── Repo functions ─────────────────────────────────────────────────────────────

def create_session(
    *,
    account_id: str,
    refresh_token: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> dict[str, Any]:
    """Persist a new login session row. Returns the created row."""
    parsed = _parse_ua(user_agent)
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO auth_sessions
                (account_id, refresh_token, user_agent, ip_address,
                 device, browser, os, is_current, revoked)
            VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, FALSE)
            RETURNING *
            """,
            (
                account_id,
                refresh_token,
                user_agent,
                ip_address,
                parsed["device"],
                parsed["browser"],
                parsed["os"],
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else {}


def list_sessions(account_id: str) -> list[dict[str, Any]]:
    """Return all non-revoked sessions for this account, newest first."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM auth_sessions
            WHERE account_id = %s AND revoked = FALSE
            ORDER BY last_active_at DESC NULLS LAST, created_at DESC
            """,
            (account_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def revoke_session(session_id: str, account_id: str) -> bool:
    """Soft-delete a session (sets revoked=TRUE). Returns True if found."""
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE auth_sessions
               SET revoked = TRUE, is_current = FALSE
             WHERE id = %s AND account_id = %s AND revoked = FALSE
            """,
            (session_id, account_id),
        )
        affected = cur.rowcount
    conn.commit()
    return affected > 0


def revoke_all_other_sessions(account_id: str) -> int:
    """Revoke every non-current session for the account."""
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE auth_sessions
               SET revoked = TRUE, is_current = FALSE
             WHERE account_id = %s AND revoked = FALSE AND is_current = FALSE
            """,
            (account_id,),
        )
        affected = cur.rowcount
    conn.commit()
    return affected


def _row_to_record(row: dict[str, Any], current_account_id: str) -> dict[str, Any]:
    """Convert a DB row into the UserSessionRecord shape the FE expects."""
    return {
        "id": str(row["id"]),
        "device": row.get("device") or "Desktop",
        "device_name": row.get("device") or "Desktop",
        "browser": row.get("browser") or "Browser",
        "browser_name": row.get("browser") or "Browser",
        "os": row.get("os"),
        "ip_address": row.get("ip_address"),
        "location": row.get("city") and row.get("country") and f"{row['city']}, {row['country']}",
        "city": row.get("city"),
        "country": row.get("country"),
        "user_agent": row.get("user_agent"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "last_active_at": row["last_active_at"].isoformat() if row.get("last_active_at") else None,
        "last_activity": row["last_active_at"].isoformat() if row.get("last_active_at") else None,
        "is_current": bool(row.get("is_current")),
    }


# ── Endpoint implementations (shared logic) ──────────────────────────────────

def _get_sessions_impl(user: dict) -> list[dict[str, Any]]:
    rows = list_sessions(user["id"])
    return [_row_to_record(r, user["id"]) for r in rows]


def _delete_session_impl(
    session_id: str,
    user: dict,
    request: Request,
) -> dict[str, Any]:
    ok = revoke_session(session_id, user["id"])
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found or already revoked")
    write_audit(
        actor_id=str(user["id"]),
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="session_revoked",
        target_id=session_id,
        service="auth-service",
        ip_address=request.client.host if request.client else None,
    )
    return {"ok": True, "revoked": session_id}


# ── Routes: /api/v1/sessions ──────────────────────────────────────────────────

@router.get("")
async def get_sessions(
    user: Annotated[dict, Depends(get_current_user)],
):
    """List the current user's active login sessions."""
    return _get_sessions_impl(user)


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    request: Request,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Revoke a specific session by ID."""
    return _delete_session_impl(session_id, user, request)


# ── Routes: /api/v1/auth/sessions (mirrored) ─────────────────────────────────

@auth_sessions_router.get("")
async def get_sessions_auth(
    user: Annotated[dict, Depends(get_current_user)],
):
    """List the current user's active login sessions (auth-prefixed path)."""
    return _get_sessions_impl(user)


@auth_sessions_router.delete("/{session_id}")
async def delete_session_auth(
    session_id: str,
    request: Request,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Revoke a session (auth-prefixed path)."""
    return _delete_session_impl(session_id, user, request)
