"""
Bulk CSV/Excel user import — CORE FEATURE.

  POST /api/admin/users/bulk-import?commit=<bool>&sendCredentials=<bool>

Two-phase:
  * commit=false → parse + validate, return an import_summary preview
                   (validRows w/ isDuplicate + selectable flags + errorRows). No writes.
  * commit=true  → insert selected rows ON CONFLICT(email) DO NOTHING with
                   must_change_password=TRUE, optionally email credentials,
                   write_audit, and return the full summary.

The admin's per-row checkbox selection is honoured via an optional `selectedRows`
form field (JSON list of rowNumbers) — only those rows commit.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile

from shared.audit import write_audit
from shared.bulk_import import import_summary, parse_rows, validate_rows
from shared.config import settings
from shared.deps import get_current_admin
from shared.mailer import build_credentials_body, send_bulk_email
from shared.security import generate_temp_password, hash_password

from superadmin_app import repo

router = APIRouter(tags=["superadmin-bulk"])

ALLOWED_ROLES = {"student", "faculty", "admin", "reviewer", "contributor", "employee", "member"}


def _parse_selected_rows(selected_rows: str | None) -> set[int] | None:
    if not selected_rows:
        return None
    try:
        parsed = json.loads(selected_rows)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    out: set[int] = set()
    for v in parsed:
        try:
            out.add(int(v))
        except (ValueError, TypeError):
            continue
    return out or None


@router.post("/api/admin/users/bulk-import")
async def bulk_import_users(
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
    file: UploadFile = File(...),
    commit: bool = Query(False, alias="commit"),
    send_credentials: bool = Query(True, alias="sendCredentials"),
    selectedRows: str | None = Form(None),
):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")

    try:
        rows, headers = parse_rows(file.filename or "", raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    existing = repo.existing_emails_lower()
    validation = validate_rows(
        rows, headers=headers, existing_emails=existing, allowed_roles=ALLOWED_ROLES,
    )
    if validation.get("missing_columns"):
        raise HTTPException(
            status_code=422,
            detail=f"Missing required columns: {', '.join(validation['missing_columns'])}",
        )

    valid_rows: list[dict[str, Any]] = validation["valid_rows"]
    error_rows: list[dict[str, Any]] = validation["error_rows"]

    # ── Preview phase ─────────────────────────────────────────────────────────
    if not commit:
        return import_summary(valid_rows, error_rows, committed=False, inserted=0)

    # ── Commit phase ────────────────────────────────────────────────────────────
    selected = _parse_selected_rows(selectedRows)
    to_insert = [r for r in valid_rows if selected is None or r["rowNumber"] in selected]

    # Generate temp passwords + hashes for the rows we are about to insert.
    prepared: list[dict[str, Any]] = []
    for r in to_insert:
        temp = r.get("_password") or generate_temp_password()
        prepared.append({
            **r,
            "_temp_password": temp,
            "_password_hash": hash_password(temp),
        })

    inserted = repo.bulk_insert_accounts(prepared)

    # ── Optionally email credentials to the inserted users ──────────────────────
    email_results: list[dict[str, Any]] = []
    if send_credentials and inserted:
        login_url = f"{settings.frontend_base_url.rstrip('/')}/auth/login"
        messages = []
        for u in inserted:
            text, html = build_credentials_body(
                name=u.get("name") or u.get("email"), email=u["email"],
                temp_password=u.get("_temp_password") or "", login_url=login_url,
                org_name="Glimmora",
            )
            messages.append({
                "to_email": u["email"],
                "subject": "Your Glimmora account credentials",
                "body": text, "html": html,
            })
        email_results = send_bulk_email(messages)

    write_audit(
        actor_id=admin.get("id"), actor_email=admin.get("email"), actor_role=admin.get("role"),
        action="bulk_import_users", service="superadmin-service",
        ip_address=request.client.host if request.client else None,
        extra={
            "inserted": len(inserted),
            "validRows": len(valid_rows),
            "errorRows": len(error_rows),
            "emailed": send_credentials,
        },
    )

    summary = import_summary(
        valid_rows, error_rows, committed=True, inserted=len(inserted),
        email_results=email_results,
    )
    summary["insertedUsers"] = [
        {k: v for k, v in u.items() if not k.startswith("_")} for u in inserted
    ]
    return summary
