"""Enterprise billing (invoices + payouts) and deliverable review.

Uses the generic kind-based data layer (db.create_row/list_rows/...) like the
SOW + projects routers, so invoices/deliverables get persistence + owner
scoping for free. Demo rows are seeded on first read so the UI shows data.

Routes (via Kong /api/v1/enterprise/* and /api/v1/billing /api/v1/review):
  GET    /api/v1/billing/invoices
  GET    /api/v1/billing/invoices/{invoice_id}
  POST   /api/v1/billing/invoices
  PATCH  /api/v1/billing/invoices/{invoice_id}
  GET    /api/v1/billing/summary
  GET    /api/v1/review/deliverables
  GET    /api/v1/review/deliverables/{deliverable_id}
  POST   /api/v1/review/deliverables/{deliverable_id}/decision
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException

from shared.deps import get_current_user

from enterprise_app import db

billing_router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
review_router = APIRouter(prefix="/api/v1/review", tags=["review"])


# ── Demo seeding (idempotent per owner) ───────────────────────────────────────

def _ensure_billing_demo(user: dict) -> None:
    if db.list_rows("invoice", user["id"]):
        return
    seeds = [
        {"number": "INV-2041", "projectId": "proj_demo", "status": "pending", "amount": 48000,
         "paidAmount": 0, "currency": "USD", "issuedDate": db.now_iso(), "dueDate": db.now_iso(),
         "lineItems": [{"description": "Cloud migration — milestone 1", "amount": 48000}]},
        {"number": "INV-2038", "projectId": "proj_demo", "status": "paid", "amount": 32000,
         "paidAmount": 32000, "currency": "USD", "issuedDate": db.now_iso(), "dueDate": db.now_iso(),
         "lineItems": [{"description": "Design system refresh", "amount": 32000}]},
        {"number": "INV-2035", "projectId": "proj_demo", "status": "overdue", "amount": 15000,
         "paidAmount": 0, "currency": "USD", "issuedDate": db.now_iso(), "dueDate": db.now_iso(),
         "lineItems": [{"description": "QA automation suite", "amount": 15000}]},
    ]
    for s in seeds:
        db.create_row("invoice", user, s)


def _ensure_review_demo(user: dict) -> None:
    if db.list_rows("deliverable", user["id"]):
        return
    seeds = [
        {"title": "DataTable component", "projectId": "proj_demo", "status": "pending_review",
         "contributor": "Aisha Khan", "submittedAt": db.now_iso(), "version": 1},
        {"title": "CRM webhook handler", "projectId": "proj_demo", "status": "pending_review",
         "contributor": "Diego Santos", "submittedAt": db.now_iso(), "version": 2},
        {"title": "Transaction history list", "projectId": "proj_demo", "status": "approved",
         "contributor": "Mei Lin", "submittedAt": db.now_iso(), "version": 1, "score": 4.4},
    ]
    for s in seeds:
        db.create_row("deliverable", user, s)


# ── Billing ────────────────────────────────────────────────────────────────

@billing_router.get("/invoices")
def list_invoices(user: Annotated[dict, Depends(get_current_user)]):
    _ensure_billing_demo(user)
    return {"invoices": db.list_rows("invoice", user["id"])}


@billing_router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = db.get_row("invoice", invoice_id, user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return row


@billing_router.post("/invoices", status_code=201)
def create_invoice(user: Annotated[dict, Depends(get_current_user)], body: dict = Body(default={})):
    return db.create_row("invoice", user, body)


@billing_router.patch("/invoices/{invoice_id}")
def update_invoice(invoice_id: str, user: Annotated[dict, Depends(get_current_user)],
                   body: dict = Body(default={})):
    row = db.merge_row("invoice", invoice_id, body, user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return row


@billing_router.get("/summary")
def billing_summary(user: Annotated[dict, Depends(get_current_user)]):
    _ensure_billing_demo(user)
    invoices = db.list_rows("invoice", user["id"])
    total_spent = sum(float(i.get("paidAmount", 0)) for i in invoices)
    pending = sum(float(i.get("amount", 0)) for i in invoices if i.get("status") in ("pending", "overdue"))
    return {
        "totalSpent": total_spent,
        "pendingPayments": pending,
        "escrowHeld": 0,
        "averagePaymentTime": 0,
        "activeInvoices": len([i for i in invoices if i.get("status") in ("pending", "overdue")]),
        "monthlySpend": [],
    }


# ── Deliverable review ─────────────────────────────────────────────────────

@review_router.get("/deliverables")
def list_deliverables(user: Annotated[dict, Depends(get_current_user)]):
    _ensure_review_demo(user)
    return {"deliverables": db.list_rows("deliverable", user["id"])}


@review_router.get("/deliverables/{deliverable_id}")
def get_deliverable(deliverable_id: str, user: Annotated[dict, Depends(get_current_user)]):
    row = db.get_row("deliverable", deliverable_id, user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="Deliverable not found")
    return row


@review_router.post("/deliverables/{deliverable_id}/decision")
def decide_deliverable(deliverable_id: str, user: Annotated[dict, Depends(get_current_user)],
                       body: dict = Body(default={})):
    decision = (body or {}).get("decision", "approve")
    status_map = {"approve": "approved", "reject": "rejected", "rework": "rework_requested"}
    # Validate the decision — an unknown value must NOT silently approve (which
    # could wrongly release payment). Reject with 422 instead.
    if decision not in status_map:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid decision '{decision}'. Must be one of: {', '.join(status_map)}.",
        )
    patch = {"status": status_map[decision],
             "decisionNote": (body or {}).get("note", ""), "decidedAt": db.now_iso()}
    row = db.merge_row("deliverable", deliverable_id, patch, user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="Deliverable not found")
    return row
