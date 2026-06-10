"""AI-generated SOWs — /api/v1/sows/**  (wrapped envelope)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from shared.deps import get_current_user

from enterprise_app import db
from enterprise_app.responses import ok
from enterprise_app.seed import ensure_demo_data

router = APIRouter(prefix="/api/v1/sows", tags=["sows"])


def _load(sow_id: str, owner_id: str | None = None) -> dict:
    row = db.get_row("sow", sow_id, owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="SOW not found")
    return row


@router.get("")
def list_sows(user: Annotated[dict, Depends(get_current_user)]):
    ensure_demo_data(user)
    return ok(db.list_rows("sow", user["id"]))


@router.get("/enterprise/all")
def list_enterprise_sows(user: Annotated[dict, Depends(get_current_user)]):
    """All SOWs visible to the enterprise (scoped to the owner here)."""
    ensure_demo_data(user)
    return ok(db.list_rows("sow", user["id"]))


_ADMIN_ROLES = {"admin", "superadmin", "super_admin", "plat", "platform"}


def _is_admin(user: dict) -> bool:
    return (user.get("role") or "").lower() in _ADMIN_ROLES


@router.get("/admin/all")
def list_all_sows_admin(user: Annotated[dict, Depends(get_current_user)]):
    """ALL SOWs across every owner — Glimmora platform admins only.

    Owner-scoped lists (/api/v1/sows) only show the caller's own SOWs, so the
    Super Admin Commercial gate couldn't see SOWs raised by enterprise tenants.
    This returns the full set (owner_id=None) so the admin queue can pick up any
    SOW that has reached the Commercial stage, regardless of who created it.
    """
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="Platform admin access required")
    return ok(db.list_rows("sow", None))


@router.get("/{sow_id}")
def get_sow(sow_id: str, user: Annotated[dict, Depends(get_current_user)]):
    ensure_demo_data(user)
    # Platform admins (who sign the Commercial gate) can open ANY SOW by id;
    # owners are scoped to their own. Without this, a Super Admin opening an
    # enterprise-owned SOW from the Commercial gate got "SOW not found".
    owner_scope = None if _is_admin(user) else user["id"]
    return ok(_load(sow_id, owner_scope))


@router.get("/enterprise/{sow_id}")
def get_enterprise_sow(sow_id: str, user: Annotated[dict, Depends(get_current_user)]):
    ensure_demo_data(user)
    return ok(_load(sow_id, user["id"]))


@router.post("/{sow_id}/action")
def sow_action(sow_id: str, user: Annotated[dict, Depends(get_current_user)],
               body: dict = Body(default={})):
    row = _load(sow_id, user["id"])
    action = (body or {}).get("action") or "noop"
    history = row.setdefault("actionHistory", [])
    history.append({"action": action, "at": db.now_iso(), "payload": body})
    status_map = {"approve": "approved", "reject": "rejected", "submit": "submitted",
                  "archive": "archived"}
    if action in status_map:
        row["status"] = status_map[action]
    row["updatedAt"] = db.now_iso()
    saved = db.update_row("sow", sow_id, row, user["id"])
    return ok(saved)


@router.get("/{sow_id}/hallucination-analysis")
def hallucination_analysis(sow_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(sow_id, user["id"])
    return ok({
        "sowId": sow_id,
        "layers": row.get("hallucinationLayers", []),
        "overallConfidence": 0.9,
    })


@router.get("/{sow_id}/risk-assessment")
def risk_assessment(sow_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(sow_id, user["id"])
    return ok(row.get("riskAssessment", {"overall": "low", "factors": []}))


@router.get("/{sow_id}/export/{fmt}")
def export_sow(sow_id: str, fmt: str, user: Annotated[dict, Depends(get_current_user)]):
    row = _load(sow_id, user["id"])
    fmt = (fmt or "").lower()
    title = row.get("projectTitle") or "SOW"
    if fmt in ("txt", "text", "md", "markdown"):
        lines = [f"# {title}", ""]
        for sec in row.get("sections", []):
            lines.append(f"## {sec.get('title')}")
            lines.append(str(sec.get("content", "")))
            lines.append("")
        return PlainTextResponse("\n".join(lines), media_type="text/plain")
    # json / pdf / docx fall back to the structured payload in the envelope.
    return ok({"sowId": sow_id, "format": fmt, "document": row})
