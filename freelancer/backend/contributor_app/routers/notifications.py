"""
Notifications router — /api/v1/notifications

Three endpoints wired to contributor_notifications (Postgres):
  GET  /api/v1/notifications                  → { notifications, unreadCount }
  PATCH /api/v1/notifications/{id}/read       → { updated, alreadyRead }
  POST  /api/v1/notifications/mark-all-read   → { updatedCount }

Response shapes mirror the FE NotificationSummary type exactly:
  id, kind, severity, title, body, actionUrl, actionLabel,
  resourceType, resourceId, channels, dispatchedAt, readAt
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from shared.audit import write_audit
from shared.deps import get_current_user

from contributor_app import db

router = APIRouter(prefix="/api/v1", tags=["notifications-v1"])


# ── auth helper ───────────────────────────────────────────────────────────────

def _acting_id(user: Annotated[dict, Depends(get_current_user)]) -> int:
    raw = user.get("id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid account id")


ActorId = Annotated[int, Depends(_acting_id)]


# ── row → FE shape ────────────────────────────────────────────────────────────

def _to_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a DB row to the NotificationSummary shape the FE expects."""
    # read_at: prefer the explicit read_at column; fall back to is_read sentinel
    read_at: str | None = row.get("read_at")
    if read_at is None and row.get("is_read"):
        # Legacy rows that were marked via the old is_read boolean but never
        # got a read_at timestamp — synthesise a plausible value so the FE
        # treats them as already read (readAt != null → read).
        read_at = row.get("created_at")  # already ISO string from row_to_dict

    # kind: use dedicated column if populated, else fall back to category
    kind = row.get("kind") or row.get("category") or "system.generic"

    # channels: stored as pg TEXT[] (arrives as Python list); guard for None
    raw_channels = row.get("channels")
    if isinstance(raw_channels, list):
        channels = raw_channels
    elif isinstance(raw_channels, str):
        # Safety: if stored as comma string by an old path
        channels = [c.strip() for c in raw_channels.split(",") if c.strip()]
    else:
        channels = ["in_app"]

    return {
        "id": str(row.get("id")),
        "kind": kind,
        "severity": row.get("severity") or "informational",
        "title": row.get("title") or "",
        "body": row.get("body") or "",
        "actionUrl": row.get("action_url"),
        "actionLabel": row.get("action_label"),
        "resourceType": row.get("resource_type"),
        "resourceId": row.get("resource_id"),
        "channels": channels,
        "dispatchedAt": row.get("created_at"),  # ISO string from row_to_dict
        "readAt": read_at,
    }


# ════════════════════════════════════════════════════════════════════════════
# GET /api/v1/notifications
# ════════════════════════════════════════════════════════════════════════════

@router.get("/notifications")
async def list_notifications(
    account_id: ActorId,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    unreadOnly: bool = Query(default=False),
):
    """
    Returns { notifications, unreadCount }.
    Supports ?unreadOnly=true and pagination (?page, ?page_size).
    """
    if unreadOnly:
        rows = db.fetch_all(
            """
            SELECT * FROM contributor_notifications
            WHERE account_id = %s
              AND (read_at IS NULL AND (is_read = FALSE OR is_read IS NULL))
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (account_id, page_size, (page - 1) * page_size),
        )
        # total count for unread
        count_row = db.fetch_one(
            """
            SELECT COUNT(*) AS c FROM contributor_notifications
            WHERE account_id = %s
              AND (read_at IS NULL AND (is_read = FALSE OR is_read IS NULL))
            """,
            (account_id,),
        )
        total = int((count_row or {}).get("c", 0))
    else:
        rows = db.fetch_all(
            """
            SELECT * FROM contributor_notifications
            WHERE account_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (account_id, page_size, (page - 1) * page_size),
        )
        count_row = db.fetch_one(
            "SELECT COUNT(*) AS c FROM contributor_notifications WHERE account_id = %s",
            (account_id,),
        )
        total = int((count_row or {}).get("c", 0))

    # unread count is always across ALL notifications, not just this page
    unread_row = db.fetch_one(
        """
        SELECT COUNT(*) AS c FROM contributor_notifications
        WHERE account_id = %s
          AND (read_at IS NULL AND (is_read = FALSE OR is_read IS NULL))
        """,
        (account_id,),
    )
    unread_count = int((unread_row or {}).get("c", 0))

    notifications = [_to_summary(r) for r in rows]

    return {
        "notifications": notifications,
        "unreadCount": unread_count,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ════════════════════════════════════════════════════════════════════════════
# PATCH /api/v1/notifications/{notificationId}/read
# ════════════════════════════════════════════════════════════════════════════

@router.patch("/notifications/{notification_id}/read")
async def mark_notification_read(
    account_id: ActorId,
    notification_id: int,
):
    """
    Returns { updated: bool, alreadyRead: bool }.
    If the notification doesn't belong to the caller → 404.
    """
    existing = db.fetch_one(
        "SELECT * FROM contributor_notifications WHERE id = %s AND account_id = %s",
        (notification_id, account_id),
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Notification not found")

    # already read?
    already_read = (existing.get("read_at") is not None) or bool(existing.get("is_read"))
    if already_read:
        return {"updated": False, "alreadyRead": True}

    now_iso = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        UPDATE contributor_notifications
        SET is_read = TRUE, read_at = now()
        WHERE id = %s AND account_id = %s
        """,
        (notification_id, account_id),
    )

    # Audit the mutation
    try:
        write_audit(
            actor_id=str(account_id),
            actor_email=None,
            actor_role="contributor",
            action="notification.mark_read",
            target="contributor_notifications",
            target_id=str(notification_id),
            service="contributor-service",
        )
    except Exception:  # noqa: BLE001
        pass

    return {"updated": True, "alreadyRead": False}


# ════════════════════════════════════════════════════════════════════════════
# POST /api/v1/notifications/mark-all-read
# ════════════════════════════════════════════════════════════════════════════

@router.post("/notifications/mark-all-read")
async def mark_all_notifications_read(account_id: ActorId):
    """
    Marks every unread notification read.
    Returns { updatedCount: int }.
    """
    # Count the unread rows that will be affected
    count_row = db.fetch_one(
        """
        SELECT COUNT(*) AS c FROM contributor_notifications
        WHERE account_id = %s
          AND (read_at IS NULL AND (is_read = FALSE OR is_read IS NULL))
        """,
        (account_id,),
    )
    updated_count = int((count_row or {}).get("c", 0))

    if updated_count > 0:
        db.execute(
            """
            UPDATE contributor_notifications
            SET is_read = TRUE, read_at = now()
            WHERE account_id = %s
              AND (read_at IS NULL OR is_read = FALSE OR is_read IS NULL)
            """,
            (account_id,),
        )

        # Audit the bulk mutation
        try:
            write_audit(
                actor_id=str(account_id),
                actor_email=None,
                actor_role="contributor",
                action="notification.mark_all_read",
                target="contributor_notifications",
                target_id=None,
                details=f"Marked {updated_count} notifications read",
                service="contributor-service",
            )
        except Exception:  # noqa: BLE001
            pass

    return {"updatedCount": updated_count}
