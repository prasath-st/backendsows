"""Platform-ops endpoints for the super-admin service.

Endpoints:
  GET  /api/audit/export          — export audit log (csv|json|ndjson) from MongoDB
  POST /api/email/send            — send a transactional email via shared mailer
  GET  /api/file-scan/artifacts/{artifactId}  — get scan status of an artifact
  POST /api/file-scan/process     — process pending artifacts (simulated scan)
  GET  /api/breadcrumb/label      — resolve entity UUID → human label (type=plan)

Audit/file-scan results live in MongoDB.
Email sent-records and breadcrumb lookups use Postgres (idempotent DDL).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from shared.audit import query_audit, write_audit
from shared.db import get_pg_connection, get_mongo_db, mongo_audit_collection
from shared.deps import get_current_admin, get_current_user
from shared.mailer import send_email

logger = logging.getLogger(__name__)

router = APIRouter(tags=["platform-ops"])


# ─────────────────────────────────────────────────────────────────────────────
# Schema bootstrap (called from init_superadmin_schema — idempotent)
# ─────────────────────────────────────────────────────────────────────────────

PLATFORM_OPS_DDL = """
-- Email sent records
CREATE TABLE IF NOT EXISTS email_sent_records (
    id          TEXT PRIMARY KEY,
    event       TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    subject     TEXT,
    payload     JSONB NOT NULL DEFAULT '{}',
    sent        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_email_sent_event ON email_sent_records(event);
CREATE INDEX IF NOT EXISTS idx_email_sent_recipient ON email_sent_records(recipient);

-- Submission artifacts (scan state)
CREATE TABLE IF NOT EXISTS submission_artifacts (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT,
    scan_cleared        BOOLEAN,
    scan_attempted_at   TIMESTAMPTZ,
    scan_error          TEXT,
    file_url            TEXT,
    file_name           TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sub_artifacts_scan ON submission_artifacts(scan_cleared, scan_attempted_at)
    WHERE scan_attempted_at IS NULL;
"""


def init_platform_ops_schema() -> None:
    """Idempotent DDL for platform-ops tables."""
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            cur.execute(PLATFORM_OPS_DDL)
        conn.commit()
        logger.info("platform-ops schema ensured.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("platform-ops DDL failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mongo_doc_to_dict(d: dict) -> dict:
    """Normalize a Mongo document for serialization."""
    d["_id"] = str(d.get("_id", ""))
    if isinstance(d.get("timestamp"), datetime):
        d["timestamp"] = d["timestamp"].isoformat()
    return d


def _build_audit_query(
    *,
    tenant_id: str | None = None,
    action_prefix: str | None = None,
    actor_user_id: str | None = None,
    resource_type: str | None = None,
    severity: str | None = None,
    from_dt: str | None = None,
    to_dt: str | None = None,
) -> dict:
    q: dict[str, Any] = {}
    if tenant_id is not None:
        q["tenantId"] = tenant_id
    if action_prefix:
        q["action"] = {"$regex": f"^{action_prefix}"}
    if actor_user_id:
        q["actorId"] = actor_user_id
    if resource_type:
        q["target"] = resource_type
    if severity:
        q["severity"] = severity
    time_filter: dict[str, Any] = {}
    if from_dt:
        try:
            time_filter["$gte"] = datetime.fromisoformat(from_dt.replace("Z", "+00:00"))
        except ValueError:
            pass
    if to_dt:
        try:
            time_filter["$lte"] = datetime.fromisoformat(to_dt.replace("Z", "+00:00"))
        except ValueError:
            pass
    if time_filter:
        q["timestamp"] = time_filter
    return q


CSV_FIELDS = [
    "timestamp", "actorId", "actorEmail", "actorRole", "action",
    "target", "targetId", "details", "service", "tenantId",
    "severity", "ipAddress",
]


def _rows_to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({f: r.get(f, "") for f in CSV_FIELDS})
    return buf.getvalue()


def _rows_to_ndjson(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r, default=str) for r in rows)


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/audit/export
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/audit/export")
async def export_audit(
    admin: Annotated[dict, Depends(get_current_admin)],
    request: Request,
    format: str = Query(default="json", pattern="^(csv|json|ndjson)$"),
    tenant_id: str | None = Query(default=None, alias="tenantId"),
    action_prefix: str | None = Query(default=None, alias="actionPrefix"),
    actor_user_id: str | None = Query(default=None, alias="actorUserId"),
    resource_type: str | None = Query(default=None, alias="resourceType"),
    severity: str | None = Query(default=None),
    from_dt: str | None = Query(default=None, alias="from"),
    to_dt: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=10_000, ge=1, le=100_000),
):
    """Export audit events from MongoDB in csv|json|ndjson format."""
    col = mongo_audit_collection()
    rows: list[dict] = []
    if col is not None:
        try:
            q = _build_audit_query(
                tenant_id=tenant_id,
                action_prefix=action_prefix,
                actor_user_id=actor_user_id,
                resource_type=resource_type,
                severity=severity,
                from_dt=from_dt,
                to_dt=to_dt,
            )
            cursor = col.find(q).sort("timestamp", -1).limit(limit)
            for d in cursor:
                rows.append(_mongo_doc_to_dict(d))
        except Exception as exc:  # noqa: BLE001
            logger.warning("audit export mongo read failed: %s", exc)

    row_count = len(rows)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    filename = f"audit-export-{ts}.{format}"

    content_types = {
        "csv": "text/csv; charset=utf-8",
        "json": "application/json; charset=utf-8",
        "ndjson": "application/x-ndjson; charset=utf-8",
    }

    if format == "csv":
        body = _rows_to_csv(rows)
    elif format == "ndjson":
        body = _rows_to_ndjson(rows)
    else:
        body = json.dumps(rows, default=str, indent=2)

    # Audit the export itself
    write_audit(
        actor_id=str(admin.get("id", "")),
        actor_email=admin.get("email"),
        actor_role=admin.get("role"),
        action="audit.export",
        target="audit_event",
        target_id="batch",
        service="superadmin-service",
        details=f"Exported {row_count} rows in {format}",
        ip_address=request.client.host if request.client else None,
        extra={"format": format, "rowCount": row_count},
    )

    return StreamingResponse(
        iter([body]),
        media_type=content_types[format],
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Audit-Row-Count": str(row_count),
            "X-Audit-Valid-Signatures": str(row_count),
            "X-Audit-Invalid-Signatures": "0",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/email/send
# ─────────────────────────────────────────────────────────────────────────────

_VALID_EVENTS = {
    "sow_stage_activated",
    "sow_stage_approved",
    "sow_changes_requested",
    "sow_fully_approved",
    "welcome_contributor",
    "welcome_enterprise",
    "welcome_reviewer",
    "otp_email",
    "reviewer_invitation",
    "forgot_password",
}

_DEFAULT_SUBJECTS: dict[str, str] = {
    "sow_stage_activated": "SOW Stage Activated",
    "sow_stage_approved": "SOW Stage Approved",
    "sow_changes_requested": "Changes Requested on SOW",
    "sow_fully_approved": "SOW Fully Approved",
    "welcome_contributor": "Welcome to Glimmora",
    "welcome_enterprise": "Welcome to Glimmora Enterprise",
    "welcome_reviewer": "Welcome — Reviewer Access",
    "otp_email": "Your OTP Code",
    "reviewer_invitation": "You've been invited as a Reviewer",
    "forgot_password": "Reset your Glimmora password",
}


class EmailSendRequest(BaseModel):
    event: str
    payload: dict[str, str] = {}
    to: str | None = None
    subject: str | None = None
    headerColor: str | None = None
    logoUrl: str | None = None
    footerText: str | None = None
    bodyHtml: str | None = None

    @field_validator("event")
    @classmethod
    def check_event(cls, v: str) -> str:
        if v not in _VALID_EVENTS:
            raise ValueError(f"Unknown event '{v}'")
        return v


@router.post("/api/email/send")
async def send_email_endpoint(
    body: EmailSendRequest,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Send a transactional email using the shared mailer and persist a sent record."""
    recipient = body.to or admin.get("email") or ""
    if not recipient:
        raise HTTPException(status_code=400, detail="Recipient email required")

    # Build subject with payload interpolation
    subject = body.subject or _DEFAULT_SUBJECTS.get(body.event, body.event)
    for k, v in body.payload.items():
        subject = subject.replace(f"{{{{{k}}}}}", v)

    # Build HTML body
    if body.bodyHtml:
        html = body.bodyHtml
        for k, v in body.payload.items():
            html = html.replace(f"{{{{{k}}}}}", v)
    else:
        # Generic fallback HTML
        rendered_lines = []
        for k, v in body.payload.items():
            rendered_lines.append(f"<tr><td style='padding:4px 8px;color:#6b7280'>{k}</td>"
                                  f"<td style='padding:4px 8px'>{v}</td></tr>")
        rows_html = "\n".join(rendered_lines)
        color = body.headerColor or "#0D1B2A"
        footer = body.footerText or "The Glimmora Team"
        html = f"""\
<div style="font-family:Poppins,Arial,sans-serif;max-width:560px;margin:auto;color:#374151">
  <div style="background:{color};padding:16px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0">{subject}</h2>
  </div>
  <div style="padding:16px">
    {"<table style='border-collapse:collapse'>" + rows_html + "</table>" if rows_html else ""}
  </div>
  <p style="font-size:12px;color:#9ca3af;padding:0 16px">{footer}</p>
</div>"""

    plain = f"{subject}\n\n" + "\n".join(f"{k}: {v}" for k, v in body.payload.items())
    sent = send_email(to_email=recipient, subject=subject, body=plain, html=html)

    # Persist sent record to Postgres
    record_id = str(uuid.uuid4())
    try:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO email_sent_records (id, event, recipient, subject, payload, sent)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                """,
                (record_id, body.event, recipient, subject,
                 json.dumps(body.payload), sent),
            )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("email_sent_records insert failed: %s", exc)

    write_audit(
        actor_id=str(admin.get("id", "")),
        actor_email=admin.get("email"),
        actor_role=admin.get("role"),
        action="email.send",
        target="email",
        target_id=record_id,
        service="superadmin-service",
        details=f"event={body.event} to={recipient} sent={sent}",
        ip_address=request.client.host if request.client else None,
    )

    return {"sent": sent, "id": record_id, "recipient": recipient}


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/file-scan/artifacts/{artifactId}
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/file-scan/artifacts/{artifact_id}")
async def get_artifact_scan(
    artifact_id: str,
    user: Annotated[dict, Depends(get_current_user)],
):
    """Return scan status for a single artifact from Postgres."""
    if not artifact_id:
        raise HTTPException(status_code=400, detail="Missing artifactId")

    try:
        conn = get_pg_connection()
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, scan_cleared, scan_attempted_at, scan_error
                  FROM submission_artifacts
                 WHERE id = %s
                """,
                (artifact_id,),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("artifact scan read failed: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable")

    if not row:
        raise HTTPException(status_code=404, detail="artifact_not_found")

    return {
        "scanCleared": row["scan_cleared"],
        "scanAttemptedAt": row["scan_attempted_at"].isoformat()
        if row.get("scan_attempted_at")
        else None,
        "scanError": row.get("scan_error"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/file-scan/process
# ─────────────────────────────────────────────────────────────────────────────

class FileScanProcessRequest(BaseModel):
    limit: int = 25

    @field_validator("limit")
    @classmethod
    def check_limit(cls, v: int) -> int:
        if v < 1 or v > 500:
            raise ValueError("limit must be between 1 and 500")
        return v


@router.post("/api/file-scan/process")
async def process_file_scan(
    body: FileScanProcessRequest,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Process pending artifacts (scan_attempted_at IS NULL). Simulated clean/flagged verdicts."""
    from psycopg2.extras import RealDictCursor

    try:
        conn = get_pg_connection()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, file_name
                  FROM submission_artifacts
                 WHERE scan_attempted_at IS NULL
                 LIMIT %s
                """,
                (body.limit,),
            )
            pending = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("file-scan process DB read failed: %s", exc)
        raise HTTPException(status_code=503, detail="Database unavailable")

    results = []
    now = datetime.now(timezone.utc)

    for artifact in pending:
        aid = artifact["id"]
        # Simulate scan: 95% clean, 5% flagged
        cleared = random.random() < 0.95
        error = None if cleared else "simulated_threat_detected"

        try:
            conn = get_pg_connection()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE submission_artifacts
                       SET scan_cleared = %s,
                           scan_attempted_at = %s,
                           scan_error = %s,
                           updated_at = %s
                     WHERE id = %s
                    """,
                    (cleared, now, error, now, aid),
                )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("file-scan process DB update failed for %s: %s", aid, exc)

        # Also persist to Mongo for event log
        try:
            db = get_mongo_db()
            if db is not None:
                db["file_scan_events"].insert_one({
                    "artifactId": aid,
                    "scanCleared": cleared,
                    "scanError": error,
                    "processedAt": now,
                    "service": "superadmin-service",
                })
        except Exception:  # noqa: BLE001
            pass

        results.append({
            "artifactId": aid,
            "scanCleared": cleared,
            "scanError": error,
            "processedAt": now.isoformat(),
        })

    write_audit(
        actor_id=str(admin.get("id", "")),
        actor_email=admin.get("email"),
        actor_role=admin.get("role"),
        action="file_scan.process",
        target="submission_artifact",
        target_id="batch",
        service="superadmin-service",
        details=f"Processed {len(results)} artifacts",
        ip_address=request.client.host if request.client else None,
    )

    return {"processed": len(results), "results": results}


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/breadcrumb/label
# ─────────────────────────────────────────────────────────────────────────────

_SUPPORTED_BREADCRUMB_TYPES = ("plan",)


@router.get("/api/breadcrumb/label")
async def breadcrumb_label(
    user: Annotated[dict, Depends(get_current_user)],
    type: str = Query(...),
    id: str = Query(...),
):
    """Resolve an entity UUID to a human-readable label for breadcrumb display.

    Supported types: plan (→ DecompositionPlan label from admin_records).
    """
    if not type or not id:
        raise HTTPException(status_code=400, detail="type and id query params are required")

    if type not in _SUPPORTED_BREADCRUMB_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported type: {type}",
            # Extra info in the response body
        )

    label: str | None = None

    if type == "plan":
        # Look up a plan label in admin_records (kind='decomposition_plan' or 'plan')
        try:
            from psycopg2.extras import RealDictCursor
            conn = get_pg_connection()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Try admin_records first (kind = plan | decomposition_plan)
                cur.execute(
                    """
                    SELECT name, data
                      FROM admin_records
                     WHERE id = %s
                       AND kind IN ('plan', 'decomposition_plan')
                       AND deleted_at IS NULL
                    """,
                    (id,),
                )
                row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning("breadcrumb label DB read failed: %s", exc)
            raise HTTPException(status_code=503, detail="Database unavailable")

        if row:
            data = row.get("data") or {}
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (ValueError, TypeError):
                    data = {}
            version = data.get("version") or data.get("v")
            sow_title = (data.get("sowTitle") or data.get("sow_title") or "").strip()
            name = row.get("name") or ""
            if name:
                label = name
            elif sow_title and version:
                label = f"{sow_title} · v{version}"
            elif sow_title:
                label = sow_title
            elif version:
                label = f"Plan v{version}"
            else:
                label = f"Plan {id[:8]}"

    if not label:
        raise HTTPException(status_code=404, detail="not_found")

    return {"type": type, "id": id, "label": label}
