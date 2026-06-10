"""
Compliance, Rate-Cards, Billing-Export, and Razorpay (simulated) endpoints.

Routes:
  GET  /api/v1/enterprise/compliance/consent    (json or csv)
  GET  /api/v1/enterprise/compliance/overview
  GET  /api/v1/enterprise/compliance/retention
  PUT  /api/v1/enterprise/compliance/retention
  GET  /api/v1/enterprise/rate-cards
  PUT  /api/v1/enterprise/rate-cards
  GET  /api/v1/billing/export                   (csv download)
  POST /api/v1/razorpay/create-order
  POST /api/v1/razorpay/webhook
  POST /api/v1/razorpay/payout-webhook
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from shared.audit import write_audit
from shared.deps import get_current_user
from shared.db import ensure_pg_clean, get_pg_connection
from psycopg2.extras import Json, RealDictCursor

logger = logging.getLogger(__name__)

# ── DDL (appended to enterprise schema, ensured on startup) ────────────────────
# See enterprise_app/db.py:init_enterprise_schema — we export this DDL string and
# call it from there.

COMPLIANCE_BILLING_DDL = """
CREATE TABLE IF NOT EXISTS enterprise_retention_rules (
    tenant_id   TEXT PRIMARY KEY,
    rules       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT
);

CREATE TABLE IF NOT EXISTS enterprise_rate_cards (
    tenant_id   TEXT PRIMARY KEY,
    currency    TEXT NOT NULL DEFAULT 'INR',
    config      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT
);

CREATE TABLE IF NOT EXISTS enterprise_razorpay_orders (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT,
    owner_id        TEXT,
    owner_email     TEXT,
    amount          BIGINT NOT NULL DEFAULT 0,
    currency        TEXT NOT NULL DEFAULT 'INR',
    status          TEXT NOT NULL DEFAULT 'created',
    order_type      TEXT NOT NULL DEFAULT 'payment',
    reference_id    TEXT,
    notes           JSONB NOT NULL DEFAULT '{}'::jsonb,
    payment_id      TEXT,
    payout_id       TEXT,
    paid_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ent_rzp_orders_owner  ON enterprise_razorpay_orders(owner_id);
CREATE INDEX IF NOT EXISTS idx_ent_rzp_orders_tenant ON enterprise_razorpay_orders(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ent_rzp_orders_status ON enterprise_razorpay_orders(status);
"""


def init_compliance_billing_schema() -> None:
    """Idempotently create the compliance/billing/razorpay tables."""
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(COMPLIANCE_BILLING_DDL)
    conn.commit()


# ── utility ────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_order_id() -> str:
    return "order_" + uuid.uuid4().hex[:16]


def _tenant_id(user: dict) -> str:
    """Derive a tenant identifier from the JWT claim or fall back to user id."""
    return user.get("tenant_id") or user.get("id") or "default"


# ── floor values (spec doc 06 §13) ─────────────────────────────────────────────

RETENTION_FLOORS: dict[str, dict] = {
    "audit_event":           {"floorDays": 365,  "label": "Audit Events"},
    "task_evidence":         {"floorDays": 730,  "label": "Task Evidence"},
    "submission_withdrawn":  {"floorDays": 90,   "label": "Withdrawn Submissions"},
    "mentor_notes":          {"floorDays": 180,  "label": "Mentor Notes"},
    "kyc_records":           {"floorDays": 1825, "label": "KYC Records"},
}

RETENTION_ENTITIES = list(RETENTION_FLOORS.keys())

REQUIRED_CONSENTS = ["acceptTos", "acceptCoc", "acceptPrivacy", "ndaAccepted"]

# ── routers ────────────────────────────────────────────────────────────────────

compliance_router = APIRouter(prefix="/api/v1/enterprise/compliance", tags=["compliance"])
rate_cards_router = APIRouter(prefix="/api/v1/enterprise", tags=["rate-cards"])
billing_export_router = APIRouter(prefix="/api/v1/billing", tags=["billing-export"])
razorpay_router = APIRouter(prefix="/api/v1/razorpay", tags=["razorpay"])
payouts_router = APIRouter(prefix="/api/v1/payouts", tags=["payouts"])


# ══════════════════════════════════════════════════════════════════════════════
# COMPLIANCE — /consent
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_consent_rows(
    tenant_id: str,
    search: str = "",
    only_missing: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Return contributor consent snapshots for the tenant.
    We read from the shared `contributor_profiles` table if it exists;
    otherwise fall back to an empty list (this is a multi-service DB — the
    contributor records live in the contributor service but share the same
    Neon instance).  We query defensively.
    """
    ensure_pg_clean()
    conn = get_pg_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Check table exists first
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'contributor_profiles'
                )
                """
            )
            exists = cur.fetchone()["exists"]
            if not exists:
                return []

            cur.execute(
                """
                SELECT
                    cp.owner_id            AS "contributorId",
                    cp.owner_email         AS email,
                    cp.data->>'firstName'  AS first_name,
                    cp.data->>'lastName'   AS last_name,
                    COALESCE((cp.data->>'ndaAccepted')::boolean,  false) AS "ndaAccepted",
                    COALESCE((cp.data->>'acceptTos')::boolean,    false) AS "acceptTos",
                    COALESCE((cp.data->>'acceptCoc')::boolean,    false) AS "acceptCoc",
                    COALESCE((cp.data->>'acceptPrivacy')::boolean,false) AS "acceptPrivacy",
                    COALESCE((cp.data->>'acceptFee')::boolean,    false) AS "acceptFee",
                    COALESCE((cp.data->>'acceptAhp')::boolean,    false) AS "acceptAhp",
                    COALESCE((cp.data->>'marketingOptIn')::boolean,false) AS "marketingOptIn",
                    cp.updated_at          AS "profileUpdatedAt"
                FROM contributor_profiles cp
                ORDER BY first_name, last_name
                LIMIT %s
                """,
                [min(limit, 5000)],
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("consent query failed: %s", exc)
        return []

    results: list[dict] = []
    for r in rows:
        name = f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip()
        missing = [k for k in REQUIRED_CONSENTS if not r.get(k)]
        updated = r.get("profileUpdatedAt")
        results.append(
            {
                "contributorId": r["contributorId"] or "",
                "email": r["email"] or "",
                "name": name,
                "ndaAccepted":   bool(r.get("ndaAccepted")),
                "acceptTos":     bool(r.get("acceptTos")),
                "acceptCoc":     bool(r.get("acceptCoc")),
                "acceptPrivacy": bool(r.get("acceptPrivacy")),
                "acceptFee":     bool(r.get("acceptFee")),
                "acceptAhp":     bool(r.get("acceptAhp")),
                "marketingOptIn":bool(r.get("marketingOptIn")),
                "profileUpdatedAt": updated.isoformat() if hasattr(updated, "isoformat") else (updated or None),
                "missingRequired": missing,
                "isComplete": len(missing) == 0,
            }
        )

    if search:
        q = search.lower()
        results = [
            r for r in results
            if q in r["email"].lower()
            or q in r["name"].lower()
            or q in r["contributorId"].lower()
        ]
    if only_missing:
        results = [r for r in results if not r["isComplete"]]
    return results


def _rows_to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "contributor_id", "email", "name", "nda", "tos", "coc", "privacy",
        "fee", "ahp", "marketing_opt_in", "profile_updated_at",
        "missing_required", "is_complete",
    ])
    for r in rows:
        writer.writerow([
            r["contributorId"], r["email"], r["name"],
            r["ndaAccepted"], r["acceptTos"], r["acceptCoc"], r["acceptPrivacy"],
            r["acceptFee"], r["acceptAhp"], r["marketingOptIn"],
            r.get("profileUpdatedAt") or "",
            "|".join(r["missingRequired"]),
            r["isComplete"],
        ])
    return buf.getvalue()


@compliance_router.get("/consent")
def get_consent(
    user: Annotated[dict, Depends(get_current_user)],
    format: str = Query(default="json"),
    search: str = Query(default=""),
    missing: str = Query(default="false"),
    limit: int = Query(default=100, ge=1, le=500),
):
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format must be 'json' or 'csv'")

    tenant_id = _tenant_id(user)
    csv_limit = 5000 if format == "csv" else limit
    rows = _fetch_consent_rows(
        tenant_id,
        search=search.strip(),
        only_missing=(missing.lower() == "true"),
        limit=csv_limit,
    )

    if format == "csv":
        csv_body = _rows_to_csv(rows)
        filename = f"glimmora_consent_{tenant_id}_{int(datetime.now(timezone.utc).timestamp())}.csv"
        return StreamingResponse(
            iter([csv_body]),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Consent-Row-Count": str(len(rows)),
            },
        )

    total = len(rows)
    paged = rows[:limit]
    complete_count = sum(1 for r in paged if r["isComplete"])
    return {
        "tenantId": tenant_id,
        "total": total,
        "complete": complete_count,
        "missing": total - complete_count,
        "rows": paged,
    }


# ══════════════════════════════════════════════════════════════════════════════
# COMPLIANCE — /overview
# ══════════════════════════════════════════════════════════════════════════════

@compliance_router.get("/overview")
def get_compliance_overview(user: Annotated[dict, Depends(get_current_user)]):
    tenant_id = _tenant_id(user)
    rows = _fetch_consent_rows(tenant_id, limit=5000)
    total = len(rows)
    with_consent = sum(1 for r in rows if r.get("ndaAccepted"))
    missing = total - with_consent

    # Deletion-request counts from Mongo audit log (fail-open)
    pending_deletions = 0
    completed_deletions = 0
    try:
        from shared.db import mongo_audit_collection
        col = mongo_audit_collection()
        if col is not None:
            deletion_actions_opened = [
                "user.delete_request.opened", "user.deletion.opened",
                "data.delete_request.opened", "contributor.gdpr_delete.opened",
            ]
            deletion_actions_completed = [
                "user.delete_request.completed", "user.deletion.completed",
                "data.delete_request.completed", "contributor.gdpr_delete.completed",
            ]
            pending_deletions = col.count_documents({"action": {"$in": deletion_actions_opened}})
            from datetime import timedelta
            thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
            completed_deletions = col.count_documents({
                "action": {"$in": deletion_actions_completed},
                "timestamp": {"$gte": thirty_days_ago},
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning("deletion count from mongo failed: %s", exc)

    # Retention display — from stored rules or floor defaults
    retention_rules = _get_retention_rules(tenant_id)
    rules = retention_rules.get("rules") or {}

    def _format_rule(entity: str) -> str:
        rule = rules.get(entity)
        if rule and rule.get("mode") == "indefinite":
            return "indefinite"
        if rule and rule.get("mode") == "days" and rule.get("days"):
            return f"{rule['days']} days"
        floor = RETENTION_FLOORS.get(entity, {}).get("floorDays", 365)
        return f"{floor} days (floor)"

    return {
        "tenantId": tenant_id,
        "consent": {
            "totalContributors": total,
            "withConsent": with_consent,
            "missingConsent": missing,
        },
        "retention": {
            "auditEvents":           _format_rule("audit_event"),
            "taskEvidence":          _format_rule("task_evidence"),
            "withdrawnSubmissions":  _format_rule("submission_withdrawn"),
        },
        "deletionRequests": {
            "pending": pending_deletions,
            "completedLast30Days": completed_deletions,
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# COMPLIANCE — /retention  GET + PUT
# ══════════════════════════════════════════════════════════════════════════════

def _get_retention_rules(tenant_id: str) -> dict:
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT rules FROM enterprise_retention_rules WHERE tenant_id = %s ORDER BY updated_at DESC LIMIT 1",
            [tenant_id],
        )
        row = cur.fetchone()
    return {"rules": dict(row["rules"]) if row else None}


def _upsert_retention_rules(tenant_id: str, rules: dict, updated_by: str | None = None) -> dict:
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO enterprise_retention_rules (tenant_id, rules, updated_at, updated_by)
            VALUES (%s, %s, now(), %s)
            ON CONFLICT (tenant_id) DO UPDATE
              SET rules      = EXCLUDED.rules,
                  updated_at = now(),
                  updated_by = EXCLUDED.updated_by
            RETURNING rules
            """,
            [tenant_id, Json(rules), updated_by],
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row["rules"]) if row else rules


@compliance_router.get("/retention")
def get_retention(user: Annotated[dict, Depends(get_current_user)]):
    tenant_id = _tenant_id(user)
    stored = _get_retention_rules(tenant_id)
    return {
        "tenantId": tenant_id,
        "rules": stored.get("rules"),
        "floors": RETENTION_FLOORS,
    }


@compliance_router.put("/retention")
def put_retention(
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
):
    tenant_id = _tenant_id(user)
    allowed_entities = set(RETENTION_ENTITIES)

    # Validate submitted entities
    violations: list[dict] = []
    clean: dict[str, Any] = {}
    for entity, rule in (body or {}).items():
        if entity not in allowed_entities:
            raise HTTPException(status_code=400, detail=f"Unknown retention entity: {entity}")
        if not isinstance(rule, dict):
            raise HTTPException(status_code=400, detail=f"Rule for {entity} must be an object")
        mode = rule.get("mode")
        if mode not in ("indefinite", "days"):
            raise HTTPException(status_code=400, detail=f"mode for {entity} must be 'indefinite' or 'days'")
        if mode == "days":
            days = rule.get("days")
            if not isinstance(days, int) or days < 1:
                raise HTTPException(status_code=400, detail=f"days for {entity} must be a positive integer")
            floor = RETENTION_FLOORS.get(entity, {}).get("floorDays", 1)
            if days < floor:
                violations.append({"entity": entity, "floorDays": floor, "submittedDays": days})
        clean[entity] = rule

    if violations:
        raise HTTPException(
            status_code=422,
            detail={"error": "Retention below platform floor", "violations": violations},
        )

    old_rules = _get_retention_rules(tenant_id).get("rules")
    saved = _upsert_retention_rules(tenant_id, clean, updated_by=user.get("email"))

    # Audit
    write_audit(
        actor_id=user.get("id"),
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="retention_rules.update",
        target="tenant",
        target_id=tenant_id,
        tenant_id=tenant_id,
        extra={"before": old_rules, "after": clean},
    )

    return {
        "tenantId": tenant_id,
        "rules": saved,
        "floors": RETENTION_FLOORS,
    }


# ══════════════════════════════════════════════════════════════════════════════
# RATE CARDS  GET + PUT
# ══════════════════════════════════════════════════════════════════════════════

def _get_rate_cards(tenant_id: str) -> dict | None:
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT currency, config FROM enterprise_rate_cards WHERE tenant_id = %s ORDER BY updated_at DESC LIMIT 1",
            [tenant_id],
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"currency": row["currency"], **dict(row["config"])}


def _upsert_rate_cards(tenant_id: str, currency: str, config: dict, updated_by: str | None = None) -> dict:
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO enterprise_rate_cards (tenant_id, currency, config, updated_at, updated_by)
            VALUES (%s, %s, %s, now(), %s)
            ON CONFLICT (tenant_id) DO UPDATE
              SET currency   = EXCLUDED.currency,
                  config     = EXCLUDED.config,
                  updated_at = now(),
                  updated_by = EXCLUDED.updated_by
            RETURNING currency, config
            """,
            [tenant_id, currency, Json(config), updated_by],
        )
        row = cur.fetchone()
    conn.commit()
    return {"currency": row["currency"], **dict(row["config"])} if row else {"currency": currency, **config}


@rate_cards_router.get("/rate-cards")
def get_rate_cards(user: Annotated[dict, Depends(get_current_user)]):
    tenant_id = _tenant_id(user)
    cards = _get_rate_cards(tenant_id)
    return {
        "tenantId": tenant_id,
        "tenantCurrency": (cards or {}).get("currency", "INR"),
        "rateCards": cards,
    }


@rate_cards_router.put("/rate-cards")
def put_rate_cards(
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
):
    tenant_id = _tenant_id(user)
    currency = (body or {}).get("currency", "")
    if not currency or len(currency) != 3:
        raise HTTPException(status_code=400, detail="currency must be a 3-letter ISO code")

    default_rate = (body or {}).get("default")
    if default_rate is None or not isinstance(default_rate, (int, float)) or default_rate < 0:
        raise HTTPException(status_code=400, detail="default rate must be a non-negative number")

    by_segment = body.get("bySegment") or {}

    config: dict[str, Any] = {"default": default_rate, "bySegment": by_segment}

    old_cards = _get_rate_cards(tenant_id)
    saved = _upsert_rate_cards(tenant_id, currency, config, updated_by=user.get("email"))

    write_audit(
        actor_id=user.get("id"),
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="tenant.rate_cards.update",
        target="tenant",
        target_id=tenant_id,
        tenant_id=tenant_id,
        extra={"before": old_cards, "after": saved, "currency": currency, "default": default_rate},
    )

    return {"tenantId": tenant_id, "rateCards": saved}


# ══════════════════════════════════════════════════════════════════════════════
# BILLING EXPORT   GET /api/v1/billing/export
# ══════════════════════════════════════════════════════════════════════════════

def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _build_payouts_csv(tenant_id: str, from_dt: datetime, to_dt: datetime) -> tuple[str, int]:
    """Return (csv_body, row_count) for simulated payout rows."""
    ensure_pg_clean()
    conn = get_pg_connection()
    rows: list[dict] = []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, owner_id, owner_email, amount, currency, status,
                       reference_id, payout_id, created_at, paid_at
                FROM   enterprise_razorpay_orders
                WHERE  tenant_id = %s
                  AND  order_type = 'payout'
                  AND  created_at >= %s
                  AND  created_at <= %s
                ORDER BY created_at DESC
                LIMIT 5000
                """,
                [tenant_id, from_dt, to_dt],
            )
            rows = cur.fetchall() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("payout export query failed: %s", exc)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["payout_id", "order_id", "owner_email", "amount", "currency", "status", "reference_id", "created_at", "paid_at"])
    for r in rows:
        paid_at = r["paid_at"].isoformat() if r.get("paid_at") else ""
        created_at = r["created_at"].isoformat() if r.get("created_at") else ""
        w.writerow([
            r.get("payout_id") or "", r["id"], r.get("owner_email") or "",
            r["amount"], r["currency"], r["status"],
            r.get("reference_id") or "", created_at, paid_at,
        ])
    return buf.getvalue(), len(rows)


def _build_billing_csv(tenant_id: str, from_dt: datetime, to_dt: datetime) -> tuple[str, int]:
    """Return (csv_body, row_count) for simulated billing/invoice rows."""
    ensure_pg_clean()
    conn = get_pg_connection()
    rows: list[dict] = []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id,
                       data->>'number'      AS invoice_number,
                       owner_email,
                       data->>'amount'      AS amount,
                       data->>'paidAmount'  AS paid_amount,
                       data->>'currency'    AS currency,
                       data->>'status'      AS status,
                       data->>'issuedDate'  AS issued_date,
                       data->>'dueDate'     AS due_date,
                       created_at
                FROM enterprise_invoices
                WHERE owner_id = %s
                  AND created_at >= %s
                  AND created_at <= %s
                ORDER BY created_at DESC
                LIMIT 5000
                """,
                [tenant_id, from_dt, to_dt],
            )
            rows = cur.fetchall() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("billing export query failed: %s", exc)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["invoice_id", "invoice_number", "owner_email", "amount", "paid_amount", "currency", "status", "issued_date", "due_date", "created_at"])
    for r in rows:
        created_at = r["created_at"].isoformat() if r.get("created_at") else ""
        w.writerow([
            r["id"], r.get("invoice_number") or "", r.get("owner_email") or "",
            r.get("amount") or "0", r.get("paid_amount") or "0",
            r.get("currency") or "USD", r.get("status") or "",
            r.get("issued_date") or "", r.get("due_date") or "", created_at,
        ])
    return buf.getvalue(), len(rows)


@billing_export_router.get("/export")
def billing_export(
    user: Annotated[dict, Depends(get_current_user)],
    kind: str = Query(default="payouts"),
    from_: str = Query(default=None, alias="from"),
    to: str = Query(default=None),
):
    if kind not in ("payouts", "billing"):
        raise HTTPException(status_code=400, detail="kind must be 'payouts' or 'billing'")
    if not from_ or not to:
        raise HTTPException(status_code=400, detail="from and to query params are required (ISO 8601)")

    from_dt = _parse_dt(from_)
    to_dt = _parse_dt(to)
    if from_dt is None or to_dt is None:
        raise HTTPException(status_code=400, detail="from/to must be valid ISO 8601 timestamps")

    tenant_id = _tenant_id(user)

    if kind == "payouts":
        csv_body, row_count = _build_payouts_csv(tenant_id, from_dt, to_dt)
        filename = f"glimmora_payouts_{tenant_id}_{int(datetime.now(timezone.utc).timestamp())}.csv"
    else:
        csv_body, row_count = _build_billing_csv(tenant_id, from_dt, to_dt)
        filename = f"glimmora_billing_{tenant_id}_{int(datetime.now(timezone.utc).timestamp())}.csv"

    integrity_hash = hashlib.sha256(csv_body.encode()).hexdigest()

    write_audit(
        actor_id=user.get("id"),
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="billing.export",
        target="billing",
        target_id=filename,
        tenant_id=tenant_id,
        extra={"kind": kind, "from": from_, "to": to, "rowCount": row_count, "integrityHash": integrity_hash},
    )

    return StreamingResponse(
        iter([csv_body]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Billing-Row-Count": str(row_count),
            "X-Billing-Integrity-Sha256": integrity_hash,
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# RAZORPAY (SIMULATED)
# ══════════════════════════════════════════════════════════════════════════════

def _create_order_row(
    owner: dict,
    amount: int,
    currency: str,
    order_type: str,
    reference_id: str | None,
    notes: dict,
) -> dict:
    ensure_pg_clean()
    conn = get_pg_connection()
    order_id = _new_order_id()
    tenant_id = _tenant_id(owner)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO enterprise_razorpay_orders
                (id, tenant_id, owner_id, owner_email, amount, currency,
                 status, order_type, reference_id, notes)
            VALUES (%s, %s, %s, %s, %s, %s, 'created', %s, %s, %s)
            RETURNING *
            """,
            [
                order_id, tenant_id, owner.get("id"), owner.get("email"),
                amount, currency, order_type, reference_id, Json(notes or {}),
            ],
        )
        row = dict(cur.fetchone())
    conn.commit()
    return row


def _mark_order_paid(order_id: str, payment_id: str) -> dict | None:
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE enterprise_razorpay_orders
               SET status = 'paid', payment_id = %s, paid_at = now(), updated_at = now()
             WHERE id = %s
            RETURNING *
            """,
            [payment_id, order_id],
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


def _mark_payout_paid(order_id: str, payout_id: str) -> dict | None:
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE enterprise_razorpay_orders
               SET status = 'processed', payout_id = %s, paid_at = now(), updated_at = now()
             WHERE id = %s
            RETURNING *
            """,
            [payout_id, order_id],
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


@razorpay_router.post("/create-order")
def razorpay_create_order(
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
):
    """
    Simulated Razorpay order creation.
    Body: { amount (paise/smallest unit), currency, order_type, reference_id, notes }
    Returns: { id, amount, currency, status, created_at }
    """
    amount = int((body or {}).get("amount", 0))
    currency = str((body or {}).get("currency", "INR"))
    order_type = str((body or {}).get("order_type", "payment"))
    reference_id = (body or {}).get("reference_id")
    notes = (body or {}).get("notes") or {}

    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount must be a positive integer (smallest currency unit)")

    row = _create_order_row(user, amount, currency, order_type, reference_id, notes)

    write_audit(
        actor_id=user.get("id"),
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="razorpay.order.created",
        target="razorpay_order",
        target_id=row["id"],
        tenant_id=_tenant_id(user),
        extra={"amount": amount, "currency": currency, "order_type": order_type},
    )

    created_at = row["created_at"]
    return {
        "id": row["id"],
        "amount": row["amount"],
        "currency": row["currency"],
        "status": row["status"],
        "order_type": row["order_type"],
        "reference_id": row.get("reference_id"),
        "notes": dict(row.get("notes") or {}),
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
    }


@razorpay_router.post("/webhook")
def razorpay_webhook(body: dict = Body(default={})):
    """
    Simulated Razorpay payment webhook.
    Expected payload: { event: 'payment.captured', payload: { payment: { entity: { order_id, id } } } }
    Marks the order as paid.
    """
    event = (body or {}).get("event", "")
    if event == "payment.captured":
        try:
            entity = body["payload"]["payment"]["entity"]
            order_id = entity["order_id"]
            payment_id = entity.get("id", "pay_" + uuid.uuid4().hex[:16])
        except (KeyError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid webhook payload structure")

        row = _mark_order_paid(order_id, payment_id)
        if row is None:
            # Unknown order — still return 200 to avoid Razorpay retries
            return {"status": "ignored", "reason": "order_not_found"}

        write_audit(
            actor_id=None,
            actor_email=None,
            actor_role="webhook",
            action="razorpay.payment.captured",
            target="razorpay_order",
            target_id=order_id,
            extra={"payment_id": payment_id},
        )
        return {"status": "ok", "order_id": order_id, "payment_id": payment_id}

    # Unknown event — acknowledge but no action
    return {"status": "ignored", "event": event}


@razorpay_router.post("/payout-webhook")
def razorpay_payout_webhook(body: dict = Body(default={})):
    """
    Simulated Razorpay payout webhook.
    Expected payload: { event: 'payout.processed', payload: { payout: { entity: { id, reference_id } } } }
    Marks the order/payout row as processed.
    """
    event = (body or {}).get("event", "")
    if event in ("payout.processed", "payout.reversed"):
        try:
            entity = body["payload"]["payout"]["entity"]
            payout_id = entity.get("id", "pout_" + uuid.uuid4().hex[:16])
            # reference_id is usually the order_id we created
            order_id = entity.get("reference_id") or entity.get("order_id", "")
        except (KeyError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid payout webhook payload structure")

        if order_id:
            row = _mark_payout_paid(order_id, payout_id)
        else:
            row = None

        write_audit(
            actor_id=None,
            actor_email=None,
            actor_role="webhook",
            action=f"razorpay.{event}",
            target="razorpay_order",
            target_id=order_id or payout_id,
            extra={"payout_id": payout_id, "order_id": order_id, "found": row is not None},
        )

        return {
            "status": "ok" if row else "ignored",
            "event": event,
            "payout_id": payout_id,
            "order_id": order_id,
        }

    return {"status": "ignored", "event": event}


# ══════════════════════════════════════════════════════════════════════════════
# PAYOUTS / TENANT   GET /api/v1/payouts/tenant
# ══════════════════════════════════════════════════════════════════════════════

PAYOUT_STATUSES = frozenset(
    ["eligible", "requested", "processing", "sent", "failed", "on_hold"]
)


def _ensure_payouts_table() -> None:
    """Create the enterprise_payouts table if it doesn't exist yet."""
    ensure_pg_clean()
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS enterprise_payouts (
                id              TEXT PRIMARY KEY,
                tenant_id       TEXT NOT NULL,
                contributor_id  TEXT NOT NULL,
                task_id         TEXT NOT NULL,
                submission_id   TEXT NOT NULL,
                amount_minor    BIGINT NOT NULL DEFAULT 0,
                currency        TEXT NOT NULL DEFAULT 'INR',
                status          TEXT NOT NULL DEFAULT 'eligible',
                payout_method_id TEXT,
                external_ref    TEXT,
                failure_reason  TEXT,
                computation     JSONB NOT NULL DEFAULT '{}'::jsonb,
                eligible_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                requested_at    TIMESTAMPTZ,
                processing_at   TIMESTAMPTZ,
                sent_at         TIMESTAMPTZ,
                failed_at       TIMESTAMPTZ,
                on_hold_at      TIMESTAMPTZ,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_ent_payouts_tenant ON enterprise_payouts(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_ent_payouts_status ON enterprise_payouts(status);
            """
        )
    conn.commit()


def _row_to_payout_detail(r: dict) -> dict:
    """Map DB row (snake_case) to camelCase PayoutDetail shape the FE expects."""

    def _iso(v: Any) -> str | None:
        if v is None:
            return None
        return v.isoformat() if hasattr(v, "isoformat") else str(v)

    comp = dict(r.get("computation") or {})
    return {
        "id": r["id"],
        "contributorId": r["contributor_id"],
        "taskDefinitionId": r["task_id"],
        "submissionId": r["submission_id"],
        "tenantId": r["tenant_id"],
        "amountMinor": int(r.get("amount_minor") or 0),
        "currency": r.get("currency") or "INR",
        "computation": {
            "currency": comp.get("currency", r.get("currency") or "INR"),
            "ratePerHour": comp.get("ratePerHour", 1200),
            "hoursBilled": comp.get("hoursBilled", 1),
            "amountMinor": comp.get("amountMinor", int(r.get("amount_minor") or 0)),
            "minorMultiplier": comp.get("minorMultiplier", 100),
        },
        "status": r.get("status") or "eligible",
        "payoutMethodId": r.get("payout_method_id"),
        "externalRef": r.get("external_ref"),
        "failureReason": r.get("failure_reason"),
        "eligibleAt": _iso(r.get("eligible_at")) or _iso(r.get("created_at")),
        "requestedAt": _iso(r.get("requested_at")),
        "processingAt": _iso(r.get("processing_at")),
        "sentAt": _iso(r.get("sent_at")),
        "failedAt": _iso(r.get("failed_at")),
        "onHoldAt": _iso(r.get("on_hold_at")),
        "createdAt": _iso(r.get("created_at")),
        "updatedAt": _iso(r.get("updated_at")),
    }


@payouts_router.get("/tenant")
def list_tenant_payouts(
    user: Annotated[dict, Depends(get_current_user)],
    status: str = Query(default=""),
):
    """
    GET /api/v1/payouts/tenant?status=eligible,requested,...
    Returns { items: PayoutDetail[] } for the authenticated tenant.
    """
    try:
        _ensure_payouts_table()
    except Exception as exc:  # noqa: BLE001
        logger.warning("payouts table ensure failed: %s", exc)

    tenant_id = _tenant_id(user)
    ensure_pg_clean()
    conn = get_pg_connection()

    # Parse status filter — comma-separated or single value
    statuses: list[str] = []
    if status:
        raw = [s.strip() for s in status.split(",") if s.strip()]
        statuses = [s for s in raw if s in PAYOUT_STATUSES]

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if statuses:
                cur.execute(
                    """
                    SELECT * FROM enterprise_payouts
                    WHERE tenant_id = %s AND status = ANY(%s)
                    ORDER BY updated_at DESC
                    """,
                    [tenant_id, statuses],
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM enterprise_payouts
                    WHERE tenant_id = %s
                    ORDER BY updated_at DESC
                    """,
                    [tenant_id],
                )
            rows = cur.fetchall() or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("payouts query failed: %s", exc)
        rows = []

    items = [_row_to_payout_detail(dict(r)) for r in rows]
    return {"items": items}


@payouts_router.post("/release-batch", status_code=200)
def release_payout_batch(
    user: Annotated[dict, Depends(get_current_user)],
    body: dict = Body(default={}),
):
    """
    POST /api/v1/payouts/release-batch
    Transitions all 'eligible' payouts for the tenant to 'requested'.
    Returns { releasedCount, totalMinor }.
    """
    try:
        _ensure_payouts_table()
    except Exception as exc:  # noqa: BLE001
        logger.warning("payouts table ensure failed: %s", exc)

    tenant_id = _tenant_id(user)
    ensure_pg_clean()
    conn = get_pg_connection()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE enterprise_payouts
                SET status = 'requested', requested_at = now(), updated_at = now()
                WHERE tenant_id = %s AND status = 'eligible'
                RETURNING amount_minor
                """,
                [tenant_id],
            )
            updated = cur.fetchall() or []
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("release-batch failed: %s", exc)
        conn.rollback()
        raise HTTPException(status_code=500, detail="Failed to release batch") from exc

    total_minor = sum(int(r["amount_minor"] or 0) for r in updated)
    write_audit(
        actor_id=user.get("id"),
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="payouts.release_batch",
        target="payout",
        target_id=tenant_id,
        tenant_id=tenant_id,
        extra={"releasedCount": len(updated), "totalMinor": total_minor},
    )
    return {"releasedCount": len(updated), "totalMinor": total_minor}
