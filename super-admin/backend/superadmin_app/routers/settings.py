"""
Settings — contributor pricing.

  GET  /api/v1/settings/contributor-pricing   (superadmin | enterprise_admin)
  PUT  /api/v1/settings/contributor-pricing   (superadmin only)
  GET  /api/v1/config/contributor-pricing      (PUBLIC — no auth)

Stored in contributor_pricing.data (JSONB). Shape:
  { student: {currency, hourlyRate},
    workforceSlabs: [{id, minYears, maxYears, currency, rate}] }
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from shared.audit import write_audit
from shared.deps import get_current_user

from superadmin_app import repo

router = APIRouter(tags=["superadmin-settings"])

_PRICING_READ_ROLES = {"superadmin", "super_admin", "enterprise_admin", "enterprise", "admin"}
_PRICING_WRITE_ROLES = {"superadmin", "super_admin"}


class StudentPricing(BaseModel):
    currency: str | None = None
    hourlyRate: float | None = None


class WorkforceSlab(BaseModel):
    id: str | None = None
    minYears: float | None = None
    maxYears: float | None = None
    currency: str | None = None
    rate: float | None = None


class ContributorPricing(BaseModel):
    student: StudentPricing | None = None
    workforceSlabs: list[WorkforceSlab] | None = None


# ── GET (read roles) ──────────────────────────────────────────────────────────

@router.get("/api/v1/settings/contributor-pricing")
async def get_contributor_pricing(user: Annotated[dict, Depends(get_current_user)]):
    if (user.get("role") or "").lower() not in _PRICING_READ_ROLES:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return {"data": repo.get_contributor_pricing()}


# ── PUT (superadmin only) ─────────────────────────────────────────────────────

@router.put("/api/v1/settings/contributor-pricing")
async def put_contributor_pricing(
    body: ContributorPricing,
    request: Request,
    user: Annotated[dict, Depends(get_current_user)],
):
    if (user.get("role") or "").lower() not in _PRICING_WRITE_ROLES:
        raise HTTPException(status_code=403, detail="Super-admin access required")
    data: dict[str, Any] = body.model_dump(exclude_none=False)
    saved = repo.set_contributor_pricing(data)
    write_audit(actor_id=user.get("id"), actor_email=user.get("email"), actor_role=user.get("role"),
                action="update_contributor_pricing", service="superadmin-service",
                ip_address=request.client.host if request.client else None)
    return {"data": saved}


# ── GET public config (NO auth) ───────────────────────────────────────────────

@router.get("/api/v1/config/contributor-pricing")
async def public_contributor_pricing():
    return {"data": repo.get_contributor_pricing()}
