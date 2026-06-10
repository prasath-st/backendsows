"""Users — /api/v1/users/**  (wrapped envelope)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, File, UploadFile
from psycopg2.extras import RealDictCursor

from shared.blob import blob_is_configured, upload_blob
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_user

from enterprise_app import db
from enterprise_app.responses import ok

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("/search")
def search_users(user: Annotated[dict, Depends(get_current_user)],
                 q: str = "", organisation: str = ""):
    """Search login_accounts by name/email, optionally by org. Best-effort."""
    results: list[dict] = []
    try:
        ensure_pg_clean()
        conn = get_pg_connection()
        clauses = []
        params: list = []
        if q:
            clauses.append("(email ILIKE %s OR first_name ILIKE %s OR last_name ILIKE %s)")
            like = f"%{q}%"
            params.extend([like, like, like])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT id, email, first_name, last_name, role FROM login_accounts{where} "
                f"ORDER BY created_at DESC LIMIT 25",
                params,
            )
            rows = cur.fetchall()
        for r in rows:
            results.append({
                "id": r["id"],
                "email": r["email"],
                "name": f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip(),
                "role": r.get("role"),
                "organisation": organisation or None,
            })
    except Exception:  # noqa: BLE001
        results = []
    return ok(results)


@router.put("/me/profile")
def update_my_profile(user: Annotated[dict, Depends(get_current_user)],
                      body: dict = Body(default={})):
    saved = db.upsert_profile(user, dict(body or {}))
    return ok(saved)


@router.post("/me/profile-picture")
async def upload_profile_picture(user: Annotated[dict, Depends(get_current_user)],
                                 file: UploadFile = File(...)):
    raw = await file.read()
    url = None
    if blob_is_configured():
        try:
            res = await upload_blob(
                pathname=f"enterprise/profile-pics/{user['id']}-{file.filename}",
                data=raw,
                content_type=file.content_type,
                add_random_suffix=True,
            )
            url = res.get("url")
        except Exception:  # noqa: BLE001
            url = None
    profile = db.get_profile(user["id"]) or {}
    saved = db.upsert_profile(user, profile, picture_url=url)
    return ok({"pictureUrl": url, "profile": saved})
