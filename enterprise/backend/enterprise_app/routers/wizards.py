"""Wizards — /api/v1/wizards/**  (wrapped envelope)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException

from shared.deps import get_current_user

from enterprise_app import db
from enterprise_app.responses import ok
from enterprise_app.seed import ensure_demo_data

router = APIRouter(prefix="/api/v1/wizards", tags=["wizards"])

TOTAL_STEPS = 9


def _new_wizard(owner: dict, body: dict) -> dict:
    wid = db.new_id("wiz_")
    steps = {str(i): {"step": i, "status": "pending", "data": {}} for i in range(1, TOTAL_STEPS + 1)}
    payload = {
        "id": wid,
        "status": "in_progress",
        "currentStep": 1,
        "totalSteps": TOTAL_STEPS,
        "steps": steps,
        "title": (body or {}).get("title") or "Untitled Wizard",
        "data": dict(body or {}),
        "createdAt": db.now_iso(),
        "updatedAt": db.now_iso(),
    }
    return payload


@router.get("")
def list_wizards(user: Annotated[dict, Depends(get_current_user)]):
    return ok(db.list_rows("wizard", user["id"]))


@router.post("")
def create_wizard(user: Annotated[dict, Depends(get_current_user)],
                  body: dict = Body(default={})):
    payload = _new_wizard(user, body)
    saved = db.create_row("wizard", user, payload, row_id=payload["id"])
    return ok(saved)


@router.get("/{wizard_id}")
def get_wizard(wizard_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = db.get_row("wizard", wizard_id, user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="Wizard not found")
    return ok(row)


@router.put("/{wizard_id}/steps/{step}")
def update_step(wizard_id: str, step: int, user: Annotated[dict, Depends(get_current_user)],
                body: dict = Body(default={})):
    row = db.get_row("wizard", wizard_id, user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="Wizard not found")
    steps = row.setdefault("steps", {})
    steps[str(step)] = {"step": step, "status": "complete", "data": dict(body or {})}
    row["currentStep"] = min(step + 1, TOTAL_STEPS)
    row["updatedAt"] = db.now_iso()
    saved = db.update_row("wizard", wizard_id, row, user["id"])
    return ok(saved)


@router.post("/{wizard_id}/steps/{step}/skip")
def skip_step(wizard_id: str, step: int, user: Annotated[dict, Depends(get_current_user)]):
    row = db.get_row("wizard", wizard_id, user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="Wizard not found")
    steps = row.setdefault("steps", {})
    steps[str(step)] = {"step": step, "status": "skipped", "data": {}}
    row["currentStep"] = min(step + 1, TOTAL_STEPS)
    row["updatedAt"] = db.now_iso()
    saved = db.update_row("wizard", wizard_id, row, user["id"])
    return ok(saved)


@router.get("/{wizard_id}/steps/9/summary")
def step_summary(wizard_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = db.get_row("wizard", wizard_id, user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="Wizard not found")
    steps = row.get("steps", {})
    completed = [s for s in steps.values() if s.get("status") == "complete"]
    skipped = [s for s in steps.values() if s.get("status") == "skipped"]
    summary = {
        "wizardId": wizard_id,
        "totalSteps": TOTAL_STEPS,
        "completed": len(completed),
        "skipped": len(skipped),
        "steps": steps,
        "ready": len(completed) + len(skipped) >= TOTAL_STEPS,
    }
    return ok(summary)


@router.post("/{wizard_id}/generate")
def generate_from_wizard(wizard_id: str, user: Annotated[dict, Depends(get_current_user)]):
    """Generate an AI SOW from a completed wizard and persist it."""
    ensure_demo_data(user)
    wiz = db.get_row("wizard", wizard_id, user["id"])
    if wiz is None:
        raise HTTPException(status_code=404, detail="Wizard not found")

    sow_id = db.new_id("sow_")
    merged_data: dict[str, Any] = {}
    for s in wiz.get("steps", {}).values():
        merged_data.update(s.get("data") or {})
    sow_payload = {
        "id": sow_id,
        "source": "ai",
        "wizardId": wizard_id,
        "projectTitle": merged_data.get("projectTitle") or wiz.get("title") or "Generated SOW",
        "clientOrganisation": merged_data.get("clientOrganisation") or "",
        "status": "generated",
        "createdAt": db.now_iso(),
        "updatedAt": db.now_iso(),
        "data": merged_data,
        "sections": merged_data.get("sections", []),
        "approvalStages": [
            {"key": "legal", "title": "Legal Review", "status": "pending"},
            {"key": "finance", "title": "Finance Review", "status": "pending"},
            {"key": "exec", "title": "Executive Sign-off", "status": "pending"},
        ],
    }
    db.create_row("sow", user, sow_payload, row_id=sow_id, extra_cols={"source": "ai"})

    wiz["status"] = "completed"
    wiz["generatedSowId"] = sow_id
    wiz["updatedAt"] = db.now_iso()
    db.update_row("wizard", wizard_id, wiz, user["id"])
    return ok({"wizardId": wizard_id, "sowId": sow_id, "sow": sow_payload})
