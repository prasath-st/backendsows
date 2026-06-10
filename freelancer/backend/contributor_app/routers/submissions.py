"""
Submissions API — /api/v1/submissions/*

Lifecycle:
  draft → submitted → under_review → (feedback_requested ↔ resubmitted)* → accepted | rejected

POST /api/v1/submissions            create draft
GET  /api/v1/submissions            list (with ?status= / ?taskId= / ?limit=)
GET  /api/v1/submissions/{id}       detail
PATCH /api/v1/submissions/{id}      update draft (body / payload)
POST /api/v1/submissions/{id}/submit    transition → submitted, route to mentor
POST /api/v1/submissions/{id}/withdraw  transition → draft (pull back)
POST /api/v1/submissions/{id}/artifacts          attach artifact
DELETE /api/v1/submissions/{id}/artifacts/{aid}  remove artifact
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from psycopg2.extras import Json

from shared.audit import write_audit
from shared.deps import get_current_user
from shared.kafka_bus import publish_event

from contributor_app import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/submissions", tags=["submissions-v1"])

# ── Auth dependency ───────────────────────────────────────────────────────────

def _acting_account_id(user: Annotated[dict, Depends(get_current_user)]) -> int:
    raw = user.get("id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid account id in token")


AcctId = Annotated[int, Depends(_acting_account_id)]

_now = lambda: datetime.now(timezone.utc).isoformat()


# ── Schema (idempotent) ───────────────────────────────────────────────────────

SUBMISSIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS submissions (
    id                  BIGSERIAL PRIMARY KEY,
    account_id          BIGINT NOT NULL,
    task_definition_id  TEXT NOT NULL,
    tenant_id           TEXT,
    version             INT NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'draft',
    body                TEXT,
    payload             JSONB DEFAULT '{}',
    reviewer_id         TEXT,
    reviewer_assigned_at TIMESTAMPTZ,
    submitted_at        TIMESTAMPTZ,
    decided_at          TIMESTAMPTZ,
    ai_suggested_decision TEXT,
    ai_invocation_id    TEXT,
    decision_rationale  TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_submissions_account ON submissions(account_id);
CREATE INDEX IF NOT EXISTS idx_submissions_task    ON submissions(task_definition_id);
CREATE INDEX IF NOT EXISTS idx_submissions_status  ON submissions(status);

CREATE TABLE IF NOT EXISTS submission_artifacts (
    id              BIGSERIAL PRIMARY KEY,
    submission_id   BIGINT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'link',
    name            TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL DEFAULT '',
    mime_type       TEXT,
    size_bytes      BIGINT DEFAULT 0,
    caption         TEXT,
    scan_cleared    BOOLEAN NOT NULL DEFAULT TRUE,
    scan_attempted_at TIMESTAMPTZ,
    scan_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sub_artifacts_sub ON submission_artifacts(submission_id);
"""


def ensure_submissions_schema() -> None:
    """Idempotent DDL — called from init_contributor_schema."""
    c = db.conn()
    with c.cursor() as cur:
        cur.execute(SUBMISSIONS_SCHEMA_SQL)
    c.commit()
    logger.info("submissions schema ensured.")


# ── Serialisers ───────────────────────────────────────────────────────────────

def _serialize_artifact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "kind": row.get("kind", "link"),
        "name": row.get("name", ""),
        "url": row.get("url", ""),
        "mimeType": row.get("mime_type"),
        "sizeBytes": row.get("size_bytes"),
        "caption": row.get("caption"),
        "scanCleared": bool(row.get("scan_cleared", True)),
        "scanAttemptedAt": _iso(row.get("scan_attempted_at")),
        "scanError": row.get("scan_error"),
        "createdAt": _iso(row.get("created_at")),
    }


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _serialize_submission(row: dict[str, Any], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    acct = db.get_account(row["account_id"]) or {}
    return {
        "id": str(row["id"]),
        "taskDefinitionId": row.get("task_definition_id") or "",
        "contributorId": str(row["account_id"]),
        "tenantId": row.get("tenant_id") or "",
        "version": row.get("version", 1),
        "status": row.get("status", "draft"),
        "body": row.get("body"),
        "payload": row.get("payload") if isinstance(row.get("payload"), dict) else {},
        "reviewerId": row.get("reviewer_id"),
        "reviewerAssignedAt": _iso(row.get("reviewer_assigned_at")),
        "submittedAt": _iso(row.get("submitted_at")),
        "decidedAt": _iso(row.get("decided_at")),
        "aiSuggestedDecision": row.get("ai_suggested_decision"),
        "aiInvocationId": row.get("ai_invocation_id"),
        "decisionRationale": row.get("decision_rationale"),
        "createdAt": _iso(row.get("created_at")),
        "updatedAt": _iso(row.get("updated_at")),
        "artifacts": [_serialize_artifact(a) for a in artifacts],
        # Extra context useful to FE
        "_contributorEmail": acct.get("email"),
        "_contributorName": acct.get("name") or acct.get("email"),
    }


def _serialize_summary(row: dict[str, Any], artifact_count: int) -> dict[str, Any]:
    acct = db.get_account(row["account_id"]) or {}
    return {
        "id": str(row["id"]),
        "taskDefinitionId": row.get("task_definition_id") or "",
        "taskTitle": row.get("task_title") or "",
        "contributorId": str(row["account_id"]),
        "contributorName": acct.get("name") or acct.get("email") or f"Contributor {row['account_id']}",
        "version": row.get("version", 1),
        "status": row.get("status", "draft"),
        "submittedAt": _iso(row.get("submitted_at")),
        "reviewerId": row.get("reviewer_id"),
        "artifactCount": artifact_count,
    }


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_submission_row(submission_id: int, account_id: int) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM submissions WHERE id=%s AND account_id=%s",
        (submission_id, account_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    return row


def _get_artifacts(submission_id: int) -> list[dict[str, Any]]:
    return db.fetch_all(
        "SELECT * FROM submission_artifacts WHERE submission_id=%s ORDER BY created_at ASC",
        (submission_id,),
    )


def _load_detail(submission_id: int, account_id: int) -> dict[str, Any]:
    row = _get_submission_row(submission_id, account_id)
    artifacts = _get_artifacts(submission_id)
    return _serialize_submission(row, artifacts)


# ── Mentor routing helper (mirrors existing task-submission flow) ──────────────

def _route_to_mentor(account_id: int, submission_id: int, task_definition_id: str,
                     body: str | None, payload: dict) -> None:
    """Create a mentor_reviews row so the submission enters the mentor queue.
    Non-fatal — failures are swallowed."""
    try:
        acct = db.fetch_one(
            "SELECT email, first_name, last_name, role, department FROM login_accounts WHERE id=%s",
            (account_id,),
        )
        is_internal = bool(acct and (acct.get("role") == "employee"
                                     or (acct.get("department") or "").lower() == "internal"))
        if is_internal:
            return  # internal employees skip mentor queue

        name = (
            f"{(acct or {}).get('first_name') or ''} {(acct or {}).get('last_name') or ''}".strip()
            or (acct or {}).get("email") or f"Contributor {account_id}"
        )
        title = (payload.get("taskTitle") or payload.get("title")
                 or task_definition_id or "Task submission")

        # Try to find SOW-assigned mentor from admin_records
        mentor_id, mentor_email = "pool", None
        try:
            sow_id = payload.get("sowId") or payload.get("sow_id")
            if sow_id:
                rec = db.fetch_one(
                    "SELECT data FROM admin_records WHERE kind='sow_mentor' AND name=%s "
                    "AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 1",
                    (sow_id,),
                )
                rec = rec or {}
                rdata = rec.get("data") if isinstance(rec.get("data"), dict) else rec
                if isinstance(rdata, dict) and rdata.get("mentorId"):
                    mentor_id = str(rdata["mentorId"])
                    mentor_email = rdata.get("mentorEmail")
        except Exception:  # noqa: BLE001
            mentor_id, mentor_email = "pool", None

        db.execute(
            "INSERT INTO mentor_reviews "
            "  (mentor_id, mentor_email, title, submission_type, contributor_id, contributor_name, "
            "   priority, status, payload) "
            "VALUES (%s,%s,%s,'assignment',%s,%s,'normal','pending',%s)",
            (mentor_id, mentor_email, title, str(account_id), name,
             Json({
                 "submissionId": submission_id,
                 "taskDefinitionId": task_definition_id,
                 "accountId": account_id,
                 "body": body,
                 "stage": "mentor",
                 **{k: v for k, v in payload.items() if k not in ("body",)},
             })),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("mentor routing failed for submission %s: %s", submission_id, exc)


# ════════════════════════════════════════════════════════════════════════════
# Endpoints
# ════════════════════════════════════════════════════════════════════════════

@router.get("")
async def list_submissions(
    account_id: AcctId,
    status: list[str] | None = Query(default=None),
    task_id: str | None = Query(default=None, alias="taskId"),
    limit: int | None = Query(default=None),
    page: int = 1,
    page_size: int = 20,
):
    """List submissions for the authenticated contributor.
    Returns { items: SubmissionSummary[] }.
    """
    rows = db.fetch_all(
        "SELECT * FROM submissions WHERE account_id=%s ORDER BY created_at DESC",
        (account_id,),
    )
    if status:
        rows = [r for r in rows if r.get("status") in status]
    if task_id:
        rows = [r for r in rows if r.get("task_definition_id") == task_id]
    if limit:
        rows = rows[:limit]

    # Get artifact counts in one go
    import psycopg2.extras as _pge
    sub_ids = [r["id"] for r in rows]
    counts: dict[int, int] = {}
    if sub_ids:
        c = db.conn()
        with c.cursor(cursor_factory=_pge.RealDictCursor) as cur:
            cur.execute(
                "SELECT submission_id, COUNT(*) AS cnt FROM submission_artifacts "
                "WHERE submission_id = ANY(%s) GROUP BY submission_id",
                (sub_ids,),
            )
            for cnt_row in cur.fetchall():
                counts[int(cnt_row["submission_id"])] = int(cnt_row["cnt"])

    # Enrich task_title from contributor_tasks (best-effort)
    task_titles: dict[str, str] = {}
    for r in rows:
        tdid = r.get("task_definition_id") or ""
        if tdid and tdid not in task_titles:
            try:
                tid_int = int(tdid)
                t = db.fetch_one("SELECT title FROM contributor_tasks WHERE id=%s AND account_id=%s",
                                 (tid_int, account_id))
                task_titles[tdid] = (t or {}).get("title") or ""
            except (ValueError, Exception):  # noqa: BLE001
                task_titles[tdid] = ""
        r["task_title"] = task_titles.get(tdid, "")

    items = [_serialize_summary(r, counts.get(r["id"], 0)) for r in rows]
    if limit:
        return {"items": items}
    result = db.paginate(items, page, page_size)
    # Ensure FE contract: key is "items"
    return result


@router.post("", status_code=201)
async def create_draft(account_id: AcctId, payload: dict = Body(default={})):
    """Create a new draft submission.
    Body: { taskDefinitionId, body?, payload? }
    Returns { submission: SubmissionDetail }
    """
    task_definition_id = (
        payload.get("taskDefinitionId") or payload.get("task_definition_id")
        # accept taskId / task_id as a fallback — callers commonly send the task id
        or payload.get("taskId") or payload.get("task_id") or ""
    )
    if not task_definition_id:
        raise HTTPException(status_code=422, detail="taskDefinitionId (or taskId) is required")

    body_text = payload.get("body")
    extra_payload = payload.get("payload") or {}
    tenant_id = payload.get("tenantId") or payload.get("tenant_id")

    row = db.execute(
        """
        INSERT INTO submissions
            (account_id, task_definition_id, tenant_id, version, status, body, payload)
        VALUES (%s, %s, %s, 1, 'draft', %s, %s)
        RETURNING *
        """,
        (account_id, task_definition_id, tenant_id, body_text, Json(extra_payload)),
    )
    artifacts: list[dict[str, Any]] = []
    sub = _serialize_submission(row, artifacts)

    write_audit(
        actor_id=str(account_id), actor_email=None, actor_role="contributor",
        action="submission_draft_created", service="contributor-service",
        target_id=str(row["id"]),
        extra={"taskDefinitionId": task_definition_id},
    )
    publish_event("contributor.submission_draft_created",
                  {"accountId": str(account_id), "submissionId": str(row["id"]),
                   "taskDefinitionId": task_definition_id})
    return {"submission": sub}


@router.get("/{submission_id}")
async def get_submission(account_id: AcctId, submission_id: int):
    """Return { submission: SubmissionDetail }."""
    return {"submission": _load_detail(submission_id, account_id)}


@router.patch("/{submission_id}")
async def update_submission(account_id: AcctId, submission_id: int, payload: dict = Body(default={})):
    """Update draft body/payload. Returns { submission: SubmissionDetail }."""
    existing = _get_submission_row(submission_id, account_id)
    if existing.get("status") not in ("draft", "feedback_requested"):
        raise HTTPException(status_code=409,
                            detail=f"Cannot edit a submission in status '{existing.get('status')}'")

    body_text = payload.get("body", existing.get("body"))
    new_payload = payload.get("payload")
    if new_payload is not None:
        merged = {**(existing.get("payload") or {}), **new_payload}
    else:
        merged = existing.get("payload") or {}

    # Bump version on every edit so reviewers can track iterations
    new_version = int(existing.get("version", 1)) + 1

    row = db.execute(
        """
        UPDATE submissions
           SET body=%s, payload=%s, version=%s, updated_at=now()
         WHERE id=%s AND account_id=%s
        RETURNING *
        """,
        (body_text, Json(merged), new_version, submission_id, account_id),
    )
    write_audit(
        actor_id=str(account_id), actor_email=None, actor_role="contributor",
        action="submission_updated", service="contributor-service",
        target_id=str(submission_id),
    )
    return {"submission": _serialize_submission(row, _get_artifacts(submission_id))}


@router.post("/{submission_id}/submit")
async def submit_submission(account_id: AcctId, submission_id: int, payload: dict = Body(default={})):
    """Transition draft → submitted and route to mentor queue.
    Returns { submission: SubmissionDetail }.
    """
    existing = _get_submission_row(submission_id, account_id)
    allowed = {"draft", "feedback_requested", "resubmitted"}
    if existing.get("status") not in allowed:
        raise HTTPException(status_code=409,
                            detail=f"Cannot submit from status '{existing.get('status')}'")

    now = datetime.now(timezone.utc)
    # For resubmission bump version
    new_status = "submitted"
    new_version = int(existing.get("version", 1)) + 1 if existing.get("status") == "feedback_requested" else int(existing.get("version", 1))

    row = db.execute(
        """
        UPDATE submissions
           SET status='submitted', submitted_at=%s, version=%s, updated_at=now()
         WHERE id=%s AND account_id=%s
        RETURNING *
        """,
        (now, new_version, submission_id, account_id),
    )

    sub = _serialize_submission(row, _get_artifacts(submission_id))

    # Route to mentor queue (non-fatal)
    task_definition_id = row.get("task_definition_id") or ""
    body_text = row.get("body")
    sub_payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    _route_to_mentor(account_id, submission_id, task_definition_id, body_text, sub_payload)

    # Update contributor_tasks status if task_definition_id is a numeric task id
    try:
        tid = int(task_definition_id)
        db.execute(
            "UPDATE contributor_tasks SET status='submitted', updated_at=now() "
            "WHERE id=%s AND account_id=%s",
            (tid, account_id),
        )
    except (ValueError, Exception):  # noqa: BLE001
        pass

    write_audit(
        actor_id=str(account_id), actor_email=None, actor_role="contributor",
        action="submission_submitted", service="contributor-service",
        target_id=str(submission_id),
        extra={"taskDefinitionId": task_definition_id, "version": new_version},
    )
    publish_event("contributor.submission_submitted",
                  {"accountId": str(account_id), "submissionId": str(submission_id),
                   "taskDefinitionId": task_definition_id})
    return {"submission": sub}


@router.post("/{submission_id}/withdraw")
async def withdraw_submission(account_id: AcctId, submission_id: int, payload: dict = Body(default={})):
    """Pull a submission back to draft status.
    Allowed from: submitted, under_review (if not yet decided).
    Returns { submission: SubmissionDetail }.
    """
    existing = _get_submission_row(submission_id, account_id)
    allowed = {"submitted", "under_review", "resubmitted"}
    if existing.get("status") not in allowed:
        raise HTTPException(status_code=409,
                            detail=f"Cannot withdraw from status '{existing.get('status')}'")

    row = db.execute(
        """
        UPDATE submissions
           SET status='draft', updated_at=now()
         WHERE id=%s AND account_id=%s
        RETURNING *
        """,
        (submission_id, account_id),
    )

    # Revert task status if applicable
    task_definition_id = row.get("task_definition_id") or ""
    try:
        tid = int(task_definition_id)
        db.execute(
            "UPDATE contributor_tasks SET status='in_progress', updated_at=now() "
            "WHERE id=%s AND account_id=%s",
            (tid, account_id),
        )
    except (ValueError, Exception):  # noqa: BLE001
        pass

    write_audit(
        actor_id=str(account_id), actor_email=None, actor_role="contributor",
        action="submission_withdrawn", service="contributor-service",
        target_id=str(submission_id),
    )
    return {"submission": _serialize_submission(row, _get_artifacts(submission_id))}


@router.post("/{submission_id}/artifacts", status_code=201)
async def attach_artifact(account_id: AcctId, submission_id: int, payload: dict = Body(default={})):
    """Attach an artifact (file/link/evidence) to a draft submission.
    Body: { kind, name, url, mimeType?, sizeBytes?, caption?, scanCleared? }
    Returns { artifact: SubmissionArtifactDetail }
    """
    # Ownership check
    _get_submission_row(submission_id, account_id)

    kind = payload.get("kind", "link")
    if kind not in ("file", "link", "evidence"):
        raise HTTPException(status_code=422, detail="kind must be file, link, or evidence")
    name = payload.get("name") or ""
    url = payload.get("url") or ""
    if not url:
        raise HTTPException(status_code=422, detail="url is required")

    # Files start uncleared until scan; links/evidence default cleared
    scan_cleared = payload.get("scanCleared", kind != "file")

    row = db.execute(
        """
        INSERT INTO submission_artifacts
            (submission_id, kind, name, url, mime_type, size_bytes, caption, scan_cleared)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (submission_id, kind, name, url,
         payload.get("mimeType"), payload.get("sizeBytes", 0),
         payload.get("caption"), scan_cleared),
    )
    # Touch submission updated_at
    db.execute("UPDATE submissions SET updated_at=now() WHERE id=%s", (submission_id,))

    write_audit(
        actor_id=str(account_id), actor_email=None, actor_role="contributor",
        action="submission_artifact_attached", service="contributor-service",
        target_id=str(submission_id),
        extra={"artifactId": str(row["id"]), "kind": kind, "name": name},
    )
    return {"artifact": _serialize_artifact(row)}


@router.delete("/{submission_id}/artifacts/{artifact_id}")
async def remove_artifact(account_id: AcctId, submission_id: int, artifact_id: int):
    """Remove an artifact. Returns { removed: true }."""
    # Ownership check
    _get_submission_row(submission_id, account_id)

    row = db.execute(
        "DELETE FROM submission_artifacts WHERE id=%s AND submission_id=%s RETURNING id",
        (artifact_id, submission_id),
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    db.execute("UPDATE submissions SET updated_at=now() WHERE id=%s", (submission_id,))

    write_audit(
        actor_id=str(account_id), actor_email=None, actor_role="contributor",
        action="submission_artifact_removed", service="contributor-service",
        target_id=str(submission_id),
        extra={"artifactId": str(artifact_id)},
    )
    return {"removed": True}
