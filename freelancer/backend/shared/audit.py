"""
Audit logging → MongoDB `audit_log` collection. Every privileged/mutating
action records actor, action, target, details, IP, and timestamp. Also emits
a Kafka `audit.event` so analytics can consume the same stream. Fail-open.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from shared.db import mongo_audit_collection
from shared.kafka_bus import publish_event

logger = logging.getLogger(__name__)


def write_audit(
    *,
    actor_id: str | None,
    actor_email: str | None,
    actor_role: str | None,
    action: str,
    target: str | None = None,
    target_id: str | None = None,
    details: str | None = None,
    service: str | None = None,
    tenant_id: str | None = None,
    ip_address: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write one audit row to Mongo + emit a Kafka event. Never raises."""
    doc = {
        "actorId": actor_id,
        "actorEmail": actor_email,
        "actorRole": actor_role,
        "action": action,
        "target": target,
        "targetId": target_id,
        "details": details,
        "service": service,
        "tenantId": tenant_id,
        "ipAddress": ip_address,
        "timestamp": datetime.now(timezone.utc),
        **(extra or {}),
    }
    try:
        col = mongo_audit_collection()
        if col is not None:
            col.insert_one(dict(doc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audit write failed: %s", exc)
    try:
        publish_event("audit.event", {**doc, "timestamp": doc["timestamp"].isoformat()},
                      key=actor_email or action)
    except Exception:
        pass


def query_audit(
    *,
    filters: dict[str, Any] | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Paginated audit reader for admin viewers."""
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    empty = {"items": [], "page": page, "page_size": page_size, "total": 0}
    # Fail-open: Mongo unconfigured OR unreachable (DNS/network) must not 500 the
    # admin audit viewer — return an empty page instead. write_audit is already
    # fail-open, so audit is best-effort end to end.
    try:
        col = mongo_audit_collection()
        if col is None:
            return empty
        q = filters or {}
        total = col.count_documents(q)
        cursor = col.find(q).sort("timestamp", -1).skip((page - 1) * page_size).limit(page_size)
        items = []
        for d in cursor:
            d["_id"] = str(d.get("_id"))
            if isinstance(d.get("timestamp"), datetime):
                d["timestamp"] = d["timestamp"].isoformat()
            items.append(d)
        return {"items": items, "page": page, "page_size": page_size, "total": total}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audit read failed (returning empty): %s", exc)
        return empty
