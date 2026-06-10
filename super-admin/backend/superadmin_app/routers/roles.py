"""
Platform-wide RBAC role definitions for the Super Admin Roles page.

GET /api/superadmin/roles  — returns {items: [MockRoleDef, ...]}

Each item shape (camelCase, matching MockRoleDef in roles.ts):
  code          str    e.g. "ent.admin"
  scope         str    one of: plat | ent | mentor | contributor
  description   str
  permissions   list[str]
  builtIn       bool
  membersCount  int    computed live from login_accounts.role counts;
                       falls back to seeded value when table empty

Table: superadmin_role_defs
  code TEXT PRIMARY KEY, scope TEXT, description TEXT,
  permissions JSONB, built_in BOOLEAN, seed_members_count INT

No audit needed for reads.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from psycopg2.extras import RealDictCursor

from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["superadmin-roles"])


# ── helpers ──────────────────────────────────────────────────────────────────

def _conn():
    ensure_pg_clean()
    return get_pg_connection()


# ── schema init ──────────────────────────────────────────────────────────────

_SEED_ROLES: list[dict[str, Any]] = [
    # Platform-side
    {
        "code": "plat.admin",
        "scope": "plat",
        "description": "Glimmora super-admin. Full read/write across the operations portal.",
        "permissions": ["All platform operations", "Cross-tenant audit + export", "Edit RBAC roles", "Disable AI agents", "Pause payment rails"],
        "builtIn": True,
        "membersCount": 3,
    },
    {
        "code": "plat.tsm",
        "scope": "plat",
        "description": "Tenant Success Manager — onboards and supports enterprise tenants.",
        "permissions": ["Provision tenant", "Suspend tenant", "View audit", "Read system health"],
        "builtIn": True,
        "membersCount": 4,
    },
    {
        "code": "plat.mpm",
        "scope": "plat",
        "description": "Mentor Program Manager — curates the mentor pool + skill taxonomy.",
        "permissions": ["Create / suspend mentor", "Edit competency", "Add / merge / deprecate skills", "Edit rubric templates"],
        "builtIn": True,
        "membersCount": 2,
    },
    {
        "code": "plat.tns",
        "scope": "plat",
        "description": "Trust & Safety Officer — triages safety + grievance + dispute cases.",
        "permissions": ["Triage governance cases", "KYC decision", "Suspend mentor via governance", "Forward to legal"],
        "builtIn": True,
        "membersCount": 3,
    },
    {
        "code": "plat.compliance",
        "scope": "plat",
        "description": "Compliance Officer — owns cross-tenant audit + retention evidence.",
        "permissions": ["Read all audit events", "Export audit slices", "Review consent + retention configs"],
        "builtIn": True,
        "membersCount": 2,
    },
    {
        "code": "plat.payments",
        "scope": "plat",
        "description": "Payments Operator — configures and reconciles payment rails.",
        "permissions": ["Edit payment rail config", "Pause / drain rail", "View payout queue"],
        "builtIn": True,
        "membersCount": 2,
    },
    {
        "code": "plat.partnerships",
        "scope": "plat",
        "description": "Partnership Manager — universities + women-workforce orgs.",
        "permissions": ["Add / edit university partner", "Add / edit WW partner", "View KYC for WW track"],
        "builtIn": True,
        "membersCount": 2,
    },
    {
        "code": "plat.ai",
        "scope": "plat",
        "description": "AI Operator — agent + prompt configuration.",
        "permissions": ["Enable / disable agent", "Rollback prompt", "Save new prompt version", "Test prompt in sandbox"],
        "builtIn": True,
        "membersCount": 2,
    },
    # Enterprise-side
    {
        "code": "ent.admin",
        "scope": "ent",
        "description": "Enterprise tenant administrator.",
        "permissions": ["Manage tenant users", "Configure integrations", "Edit policies", "Approve SOW", "View audit"],
        "builtIn": True,
        "membersCount": 22,
    },
    {
        "code": "ent.sponsor",
        "scope": "ent",
        "description": "Business sponsor approving SOWs at the business stage.",
        "permissions": ["Approve SOW (business)", "Decline SOW", "Comment on SOW"],
        "builtIn": True,
        "membersCount": 18,
    },
    {
        "code": "ent.pmo",
        "scope": "ent",
        "description": "Programme management office — runs delivery + decomposition.",
        "permissions": ["Edit decomposition", "Manage projects", "Reassign tasks"],
        "builtIn": True,
        "membersCount": 31,
    },
    {
        "code": "ent.finance",
        "scope": "ent",
        "description": "Finance team — invoicing, rate cards, payouts oversight.",
        "permissions": ["View invoices", "Edit rate cards", "Export billing"],
        "builtIn": True,
        "membersCount": 12,
    },
    {
        "code": "ent.reviewer",
        "scope": "ent",
        "description": "Client-side reviewer — second-stage acceptance on two-stage reviews.",
        "permissions": ["Decide on routed reviews", "View review history"],
        "builtIn": True,
        "membersCount": 19,
    },
    {
        "code": "ent.it",
        "scope": "ent",
        "description": "IT / security counterpart at the enterprise.",
        "permissions": ["Configure SSO / integrations", "View security audit"],
        "builtIn": True,
        "membersCount": 14,
    },
    {
        "code": "ent.compliance",
        "scope": "ent",
        "description": "Compliance / legal counterpart — legal SOW gate, ESG + evidence, policy.",
        "permissions": ["Approve SOW (legal)", "Manage compliance evidence", "Edit policies"],
        "builtIn": True,
        "membersCount": 9,
    },
    # Mentor-side
    {
        "code": "mentor",
        "scope": "mentor",
        "description": "Mentor — first-pass reviewer + coach.",
        "permissions": ["Decide on assigned reviews", "Hold mentorship session", "Escalate"],
        "builtIn": True,
        "membersCount": 96,
    },
    {
        "code": "mentor.senior",
        "scope": "mentor",
        "description": "Senior mentor — broader competency, can pair with other mentors.",
        "permissions": ["All mentor actions", "Visible in cross-pool"],
        "builtIn": True,
        "membersCount": 28,
    },
    {
        "code": "mentor.lead",
        "scope": "mentor",
        "description": "Pool lead — coordinates pool load + escalations.",
        "permissions": ["All mentor actions", "Pool load view", "Reassign within pool"],
        "builtIn": True,
        "membersCount": 18,
    },
    # Contributor-side
    {
        "code": "contributor",
        "scope": "contributor",
        "description": "Default contributor — does tasks and earns.",
        "permissions": ["Accept / decline assigned tasks", "Submit work", "Withdraw earnings"],
        "builtIn": True,
        "membersCount": 412,
    },
]

# Map login_accounts.role values → role codes for live membersCount.
_ROLE_TO_CODE: dict[str, str] = {
    "admin":        "plat.admin",
    "superadmin":   "plat.admin",
    "super_admin":  "plat.admin",
    "enterprise":   "ent.admin",
    "mentor":       "mentor",
    "contributor":  "contributor",
    "reviewer":     "ent.reviewer",
    "freelancer":   "contributor",
}


def init_roles_schema() -> None:
    """Create superadmin_role_defs table and seed from mock data (idempotent)."""
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS superadmin_role_defs (
                code               TEXT PRIMARY KEY,
                scope              TEXT NOT NULL,
                description        TEXT NOT NULL DEFAULT '',
                permissions        JSONB NOT NULL DEFAULT '[]',
                built_in           BOOLEAN NOT NULL DEFAULT TRUE,
                seed_members_count INT NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()

        for role in _SEED_ROLES:
            cur.execute(
                """
                INSERT INTO superadmin_role_defs
                    (code, scope, description, permissions, built_in, seed_members_count)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (code) DO NOTHING
                """,
                (
                    role["code"],
                    role["scope"],
                    role["description"],
                    json.dumps(role["permissions"]),
                    role["builtIn"],
                    role["membersCount"],
                ),
            )
        conn.commit()
    logger.info("superadmin_role_defs schema + seed OK")


# Run schema init at module import time (safe; ON CONFLICT DO NOTHING).
try:
    init_roles_schema()
except Exception as _exc:  # noqa: BLE001
    logger.warning("roles schema init deferred (DB not ready): %s", _exc)


# ── live member counts from login_accounts ───────────────────────────────────

def _live_member_counts(conn) -> dict[str, int]:
    """Return {role_code: count} from login_accounts.role column."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT role, COUNT(*) AS n FROM login_accounts GROUP BY role")
            raw: dict[str, int] = {}
            for row in cur.fetchall():
                db_role, count = row[0], int(row[1])
                code = _ROLE_TO_CODE.get((db_role or "").lower())
                if code:
                    raw[code] = raw.get(code, 0) + count
            return raw
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not fetch live role counts: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return {}


# ── GET /api/superadmin/roles ─────────────────────────────────────────────────

@router.get("/api/superadmin/roles")
async def list_roles(
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Return all platform role definitions with live membersCount.

    Response shape: {items: [MockRoleDef, ...]}
    """
    conn = _conn()

    # Fetch role definitions from DB.
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT code, scope, description, permissions, built_in, seed_members_count "
            "FROM superadmin_role_defs ORDER BY scope, code"
        )
        rows = list(cur.fetchall())

    # Live counts from login_accounts (may be empty dict on error).
    live_counts = _live_member_counts(conn)

    items: list[dict[str, Any]] = []
    for row in rows:
        code = row["code"]
        perms = row["permissions"]
        if isinstance(perms, str):
            try:
                perms = json.loads(perms)
            except (ValueError, TypeError):
                perms = []

        # Use live count if available; otherwise fall back to seeded value.
        members_count = live_counts.get(code, row["seed_members_count"])

        items.append(
            {
                "code": code,
                "scope": row["scope"],
                "description": row["description"],
                "permissions": perms if isinstance(perms, list) else [],
                "builtIn": bool(row["built_in"]),
                "membersCount": members_count,
            }
        )

    return {"items": items}
