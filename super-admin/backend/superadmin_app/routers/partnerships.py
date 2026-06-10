"""
Super-Admin · Partnerships router.

Manages two domains:
  1. University MOU partners  — MockUniversity + MockUniversityStudent cohort
  2. Women-Workforce partners — MockWWPartner  + MockWWContributor cohort

All backed by two snake_case tables in Postgres:
  partnership_universities        (one row per university MOU)
  partnership_ww_partners         (one row per women-workforce org)

Both tables carry a `cohort` JSONB column that stores the full array of
student / contributor rows so the nested shape is preserved without extra
join tables.  The schema is created idempotently at module import time and
seeded from the FE mock sample data so GET returns realistic rows immediately.

Endpoints — all under /api/superadmin/partnerships:

  Women-Workforce (13 total per spec):
    GET    /women-workforce                              list all WW partners
    GET    /women-workforce/{org_id}                    single WW partner
    POST   /women-workforce                             create WW partner
    PATCH  /women-workforce/{org_id}                   update WW partner fields
    POST   /women-workforce/{org_id}/contributors       add contributor to cohort
    POST   /women-workforce/{org_id}/contributors/{cid}/invite-sent   mark invite sent

  Universities:
    GET    /universities                                list all universities
    GET    /universities/{uni_id}                      single university
    POST   /universities                               create university
    PATCH  /universities/{uni_id}                     update university fields
    POST   /universities/{uni_id}/students             add student to cohort
    POST   /universities/{uni_id}/students/{sid}/invite-sent  mark invite sent
    POST   /universities/{uni_id}/supervisors          add supervisor

No audit on reads; all mutations call write_audit.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["superadmin-partnerships"])

# ── connection helper ──────────────────────────────────────────────────────────

def _conn():
    ensure_pg_clean()
    return get_pg_connection()


# ── tiny utilities ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _load_jsonb(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (ValueError, TypeError):
            return None
    return val


def _mint_token(prefix: str) -> str:
    return f"inv-{prefix}-{secrets.token_urlsafe(12)}"


def _slugify(text: str, prefix: str) -> str:
    import re
    base = re.sub(r"[^a-z0-9]+", "-", text.lower())[:24].strip("-")
    return f"{prefix}-{base}-{secrets.token_hex(3)}"


# ── schema init + seed ─────────────────────────────────────────────────────────

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS partnership_universities (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    country          TEXT NOT NULL DEFAULT '',
    agreement_ref    TEXT NOT NULL DEFAULT '',
    agreement_signed_at TEXT,
    students_in_flight  INTEGER NOT NULL DEFAULT 0,
    students_alumni     INTEGER NOT NULL DEFAULT 0,
    lead_contact     JSONB,
    supervisors      JSONB NOT NULL DEFAULT '[]',
    academic_recognition_rules TEXT NOT NULL DEFAULT '',
    cohort           JSONB NOT NULL DEFAULT '[]',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS partnership_ww_partners (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    country              TEXT NOT NULL DEFAULT '',
    contributors         INTEGER NOT NULL DEFAULT 0,
    programs             JSONB NOT NULL DEFAULT '[]',
    lead_contact         JSONB,
    description          TEXT NOT NULL DEFAULT '',
    peer_mentor_pairings JSONB NOT NULL DEFAULT '[]',
    cohort               JSONB NOT NULL DEFAULT '[]',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# Seed data mirrors MOCK_UNIVERSITIES and MOCK_WW_PARTNERS from the FE mock.
_SEED_UNIVERSITIES = [
    {
        "id": "u-anna",
        "name": "Anna University",
        "country": "India",
        "agreement_ref": "MOU-2026-014",
        "agreement_signed_at": "2026-01-22T00:00:00Z",
        "students_in_flight": 42,
        "students_alumni": 18,
        "lead_contact": {"name": "Dr. Sundar Krishnan", "email": "sundar@annauniv.edu", "title": "Dean, External Programs"},
        "supervisors": [
            {"name": "Prof. Lakshmi N.", "email": "lakshmi@annauniv.edu", "department": "Computer Science"},
            {"name": "Prof. Raghav K.",  "email": "raghav@annauniv.edu",  "department": "Design"},
        ],
        "academic_recognition_rules": "Up to 2 credits per semester for completed GlimmoraTeam tasks. Faculty supervisor signs off each completion.",
        "cohort": [
            {"id": "stu-1", "name": "Ramesh Kumar", "email": "ramesh.k@example.in", "rollNumber": "CS2025-041", "programme": "B.Tech CS, Year 3", "supervisorEmail": "lakshmi@annauniv.edu", "status": "active", "enrolledAt": "2026-04-10T08:00:00Z", "inviteToken": "inv-anna-ramesh01", "inviteSentAt": "2026-04-08T10:00:00Z", "registeredAt": "2026-04-10T08:00:00Z"},
            {"id": "stu-2", "name": "Priya Venkat", "email": "priya.v@annauniv.edu", "rollNumber": "CS2026-112", "programme": "B.Tech CS, Year 2", "supervisorEmail": "lakshmi@annauniv.edu", "status": "onboarding", "enrolledAt": "2026-05-26T14:00:00Z", "inviteToken": "inv-anna-priya02", "inviteSentAt": "2026-05-25T09:00:00Z", "registeredAt": "2026-05-26T14:00:00Z"},
            {"id": "stu-3", "name": "Karthik M.", "email": "karthik.m@annauniv.edu", "status": "invited", "enrolledAt": "2026-05-28T09:00:00Z", "inviteToken": "inv-anna-karthik03"},
        ],
    },
    {
        "id": "u-iitm",
        "name": "IIT Madras",
        "country": "India",
        "agreement_ref": "MOU-2026-019",
        "agreement_signed_at": "2026-02-18T00:00:00Z",
        "students_in_flight": 18,
        "students_alumni": 4,
        "lead_contact": {"name": "Dr. Aravind P.", "email": "aravind@iitm.ac.in", "title": "Faculty Coordinator"},
        "supervisors": [
            {"name": "Prof. Suresh M.", "email": "suresh@iitm.ac.in", "department": "Computer Science"},
        ],
        "academic_recognition_rules": "Pilot — credit not yet recognized; participation noted on transcript.",
        "cohort": [],
    },
    {
        "id": "u-nid",
        "name": "National Institute of Design",
        "country": "India",
        "agreement_ref": "MOU-2026-026",
        "agreement_signed_at": "2026-03-04T00:00:00Z",
        "students_in_flight": 8,
        "students_alumni": 2,
        "lead_contact": {"name": "Prof. Mridula B.", "email": "mridula@nid.edu", "title": "Dean, Communications Design"},
        "supervisors": [
            {"name": "Prof. Karthik S.", "email": "karthik@nid.edu", "department": "Industrial Design"},
        ],
        "academic_recognition_rules": "1 elective credit on completion of cohort + portfolio review.",
        "cohort": [],
    },
]

_SEED_WW_PARTNERS = [
    {
        "id": "ww-sheroes",
        "name": "Sheroes.in",
        "country": "India",
        "contributors": 18,
        "programs": ["Mentorship pairing", "Skills upgrade"],
        "lead_contact": {"name": "Sairee Chahal", "email": "partnerships@sheroes.in", "title": "Founder"},
        "description": "Largest community for women in India; partner for women-workforce contributor onboarding.",
        "peer_mentor_pairings": [
            {"contributor": "Sunita Devi", "mentor": "Priya Iyer", "since": "2026-05-20"},
            {"contributor": "Anita Ramesh", "mentor": "Fatima Nair", "since": "2026-05-18"},
        ],
        "cohort": [
            {"id": "wwc-1", "name": "Sunita Devi", "email": "sunita.d@sheroes.in", "referredBy": "Sairee Chahal", "wantsPeerMentor": True, "status": "onboarding", "enrolledAt": "2026-05-20T08:00:00Z", "inviteToken": "inv-sheroes-sunita01", "inviteSentAt": "2026-05-19T10:00:00Z", "registeredAt": "2026-05-20T08:00:00Z"},
            {"id": "wwc-2", "name": "Anita Ramesh", "email": "anita.r@example.in", "status": "invited", "enrolledAt": "2026-05-27T09:00:00Z", "inviteToken": "inv-sheroes-anita02"},
            {"id": "wwc-3", "name": "Kavitha S.", "email": "kavitha.s@sheroes.in", "status": "active", "enrolledAt": "2026-04-15T08:00:00Z", "inviteToken": "inv-sheroes-kavitha03", "inviteSentAt": "2026-04-14T09:00:00Z", "registeredAt": "2026-04-15T08:00:00Z"},
        ],
    },
    {
        "id": "ww-wwcode",
        "name": "Women Who Code",
        "country": "Global",
        "contributors": 12,
        "programs": ["Outreach", "Conference talks"],
        "lead_contact": {"name": "Alaina Percival", "email": "partnerships@womenwhocode.com", "title": "Board Director"},
        "description": "Global non-profit for women technologists; pipeline for engineering tracks.",
        "peer_mentor_pairings": [],
        "cohort": [],
    },
    {
        "id": "ww-tieblr",
        "name": "TiE Bangalore",
        "country": "India",
        "contributors": 6,
        "programs": ["Mentorship pairing"],
        "lead_contact": {"name": "Naveen Asrani", "email": "naveen@tieblr.org", "title": "Programs Director"},
        "description": "Entrepreneur network; provides senior mentors for women-led ventures.",
        "peer_mentor_pairings": [],
        "cohort": [],
    },
    {
        "id": "ww-skillsdev",
        "name": "Skills Development Council",
        "country": "India",
        "contributors": 2,
        "programs": ["Outreach"],
        "lead_contact": {"name": "Vandana Joshi", "email": "vandana@sdc-india.org", "title": "Outreach Lead"},
        "description": "Government-affiliated body; outreach into Tier-2 / Tier-3 cities.",
        "peer_mentor_pairings": [],
        "cohort": [],
    },
]


def init_partnerships_schema() -> None:
    """Create tables + seed mock data. Safe to call multiple times (idempotent)."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_DDL)
        conn.commit()

        # Seed universities
        with conn.cursor() as cur:
            for u in _SEED_UNIVERSITIES:
                cur.execute(
                    """
                    INSERT INTO partnership_universities
                        (id, name, country, agreement_ref, agreement_signed_at,
                         students_in_flight, students_alumni, lead_contact,
                         supervisors, academic_recognition_rules, cohort)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        u["id"], u["name"], u["country"], u["agreement_ref"],
                        u["agreement_signed_at"],
                        u["students_in_flight"], u["students_alumni"],
                        json.dumps(u["lead_contact"]),
                        json.dumps(u["supervisors"]),
                        u["academic_recognition_rules"],
                        json.dumps(u["cohort"]),
                    ),
                )
        conn.commit()

        # Seed WW partners
        with conn.cursor() as cur:
            for w in _SEED_WW_PARTNERS:
                cur.execute(
                    """
                    INSERT INTO partnership_ww_partners
                        (id, name, country, contributors, programs, lead_contact,
                         description, peer_mentor_pairings, cohort)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        w["id"], w["name"], w["country"], w["contributors"],
                        json.dumps(w["programs"]),
                        json.dumps(w["lead_contact"]),
                        w["description"],
                        json.dumps(w["peer_mentor_pairings"]),
                        json.dumps(w["cohort"]),
                    ),
                )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("partnerships schema init failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass


# Run at import time so the tables exist before any request hits.
try:
    init_partnerships_schema()
except Exception as _exc:
    logger.warning("partnerships init skipped at import: %s", _exc)


# ── serialisers ────────────────────────────────────────────────────────────────

def _uni_out(row: dict[str, Any]) -> dict[str, Any]:
    """Map a DB row → MockUniversity shape."""
    return {
        "id": row["id"],
        "name": row["name"],
        "country": row["country"],
        "agreementRef": row["agreement_ref"],
        "agreementSignedAt": _iso(row["agreement_signed_at"]),
        "studentsInFlight": row["students_in_flight"],
        "studentsAlumni": row["students_alumni"],
        "leadContact": _load_jsonb(row["lead_contact"]) or {},
        "supervisors": _load_jsonb(row["supervisors"]) or [],
        "academicRecognitionRules": row["academic_recognition_rules"],
        "cohort": _load_jsonb(row["cohort"]) or [],
    }


def _ww_out(row: dict[str, Any]) -> dict[str, Any]:
    """Map a DB row → MockWWPartner shape."""
    return {
        "id": row["id"],
        "name": row["name"],
        "country": row["country"],
        "contributors": row["contributors"],
        "programs": _load_jsonb(row["programs"]) or [],
        "leadContact": _load_jsonb(row["lead_contact"]) or {},
        "description": row["description"],
        "peerMentorPairings": _load_jsonb(row["peer_mentor_pairings"]) or [],
        "cohort": _load_jsonb(row["cohort"]) or [],
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  WOMEN-WORKFORCE ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

# ── GET /api/superadmin/partnerships/women-workforce ──────────────────────────

@router.get("/api/superadmin/partnerships/women-workforce")
async def list_ww_partners(
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Return all women-workforce partner orgs."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM partnership_ww_partners ORDER BY created_at ASC")
        rows = cur.fetchall()
    return [_ww_out(dict(r)) for r in rows]


# ── GET /api/superadmin/partnerships/women-workforce/{org_id} ─────────────────

@router.get("/api/superadmin/partnerships/women-workforce/{org_id}")
async def get_ww_partner(
    org_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Return a single WW partner by id."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM partnership_ww_partners WHERE id = %s", (org_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "ww_partner_not_found", "orgId": org_id})
    return _ww_out(dict(row))


# ── POST /api/superadmin/partnerships/women-workforce ────────────────────────

class _CreateWWPartnerBody(BaseModel):
    name: str
    country: str
    description: str = ""
    programs: list[str] = []
    leadName: str
    leadEmail: str
    leadTitle: str


@router.post("/api/superadmin/partnerships/women-workforce", status_code=201)
async def create_ww_partner(
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _CreateWWPartnerBody = Body(...),
):
    """Create a new women-workforce partner org."""
    org_id = _slugify(body.name, "ww")
    lead = {"name": body.leadName.strip(), "email": body.leadEmail.strip().lower(), "title": body.leadTitle.strip()}
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO partnership_ww_partners
                (id, name, country, contributors, programs, lead_contact,
                 description, peer_mentor_pairings, cohort)
            VALUES (%s,%s,%s,0,%s,%s,%s,'[]','[]')
            RETURNING *
            """,
            (org_id, body.name.strip(), body.country.strip(),
             json.dumps(body.programs), json.dumps(lead), body.description.strip()),
        )
        row = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="create_ww_partner",
            target=body.name, target_id=org_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return _ww_out(dict(row))


# ── PATCH /api/superadmin/partnerships/women-workforce/{org_id} ──────────────

class _PatchWWPartnerBody(BaseModel):
    leadContact: dict | None = None
    programs: list[str] | None = None
    description: str | None = None
    contributors: int | None = None


@router.patch("/api/superadmin/partnerships/women-workforce/{org_id}")
async def patch_ww_partner(
    org_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _PatchWWPartnerBody = Body(...),
):
    """Update mutable fields on a WW partner."""
    conn = _conn()
    # Verify exists
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM partnership_ww_partners WHERE id = %s", (org_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail={"error": "ww_partner_not_found", "orgId": org_id})

    sets: list[str] = ["updated_at = NOW()"]
    params: list[Any] = []

    if body.leadContact is not None:
        sets.append("lead_contact = %s")
        params.append(json.dumps(body.leadContact))
    if body.programs is not None:
        sets.append("programs = %s")
        params.append(json.dumps(body.programs))
    if body.description is not None:
        sets.append("description = %s")
        params.append(body.description)
    if body.contributors is not None:
        sets.append("contributors = %s")
        params.append(body.contributors)

    params.append(org_id)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"UPDATE partnership_ww_partners SET {', '.join(sets)} WHERE id = %s RETURNING *",
            tuple(params),
        )
        row = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="update_ww_partner",
            target=org_id, target_id=org_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return _ww_out(dict(row))


# ── POST /api/superadmin/partnerships/women-workforce/{org_id}/contributors ───

class _AddWWContributorBody(BaseModel):
    name: str
    email: str
    status: str = "invited"
    referredBy: str | None = None
    supervisorEmail: str | None = None
    wantsPeerMentor: bool = False


@router.post("/api/superadmin/partnerships/women-workforce/{org_id}/contributors", status_code=201)
async def add_ww_contributor(
    org_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _AddWWContributorBody = Body(...),
):
    """Add a contributor to the WW partner's cohort array."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM partnership_ww_partners WHERE id = %s", (org_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "ww_partner_not_found", "orgId": org_id})

    partner = dict(row)
    cohort: list[dict] = _load_jsonb(partner["cohort"]) or []

    email = body.email.strip().lower()
    if any(c["email"] == email for c in cohort):
        raise HTTPException(status_code=409, detail={"error": "contributor_exists", "email": email})

    new_entry: dict[str, Any] = {
        "id": f"wwc-{int(time.time() * 1000)}",
        "name": body.name.strip(),
        "email": email,
        "status": body.status,
        "enrolledAt": _now_iso(),
        "inviteToken": _mint_token("ww"),
    }
    if body.referredBy:
        new_entry["referredBy"] = body.referredBy.strip()
    if body.supervisorEmail:
        new_entry["supervisorEmail"] = body.supervisorEmail.strip()
    if body.wantsPeerMentor:
        new_entry["wantsPeerMentor"] = True

    cohort.append(new_entry)
    in_flight_statuses = {"registered", "onboarding", "active"}
    in_flight = sum(1 for c in cohort if c.get("status") in in_flight_statuses)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "UPDATE partnership_ww_partners SET cohort = %s, contributors = %s, updated_at = NOW() WHERE id = %s RETURNING *",
            (json.dumps(cohort), in_flight, org_id),
        )
        updated = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="add_ww_contributor",
            target=email, target_id=org_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return {"contributor": new_entry, "partner": _ww_out(dict(updated))}


# ── POST /api/superadmin/partnerships/women-workforce/{org_id}/contributors/{cid}/invite-sent

@router.post("/api/superadmin/partnerships/women-workforce/{org_id}/contributors/{cid}/invite-sent")
async def ww_contributor_invite_sent(
    org_id: str,
    cid: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Mark a WW contributor's invite as sent (sets inviteSentAt timestamp)."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM partnership_ww_partners WHERE id = %s", (org_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "ww_partner_not_found", "orgId": org_id})

    partner = dict(row)
    cohort: list[dict] = _load_jsonb(partner["cohort"]) or []

    idx = next((i for i, c in enumerate(cohort) if c["id"] == cid), None)
    if idx is None:
        raise HTTPException(status_code=404, detail={"error": "contributor_not_found", "cid": cid})

    cohort[idx] = {**cohort[idx], "inviteSentAt": _now_iso()}

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "UPDATE partnership_ww_partners SET cohort = %s, updated_at = NOW() WHERE id = %s RETURNING *",
            (json.dumps(cohort), org_id),
        )
        updated = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="ww_contributor_invite_sent",
            target=cid, target_id=org_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return {"contributor": cohort[idx], "partner": _ww_out(dict(updated))}


# ═══════════════════════════════════════════════════════════════════════════════
#  UNIVERSITY ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

# ── GET /api/superadmin/partnerships/universities ─────────────────────────────

@router.get("/api/superadmin/partnerships/universities")
async def list_universities(
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Return all university MOU partners."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM partnership_universities ORDER BY created_at ASC")
        rows = cur.fetchall()
    return [_uni_out(dict(r)) for r in rows]


# ── GET /api/superadmin/partnerships/universities/{uni_id} ───────────────────

@router.get("/api/superadmin/partnerships/universities/{uni_id}")
async def get_university(
    uni_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Return a single university MOU by id."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM partnership_universities WHERE id = %s", (uni_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "university_not_found", "uniId": uni_id})
    return _uni_out(dict(row))


# ── POST /api/superadmin/partnerships/universities ────────────────────────────

class _CreateUniversityBody(BaseModel):
    name: str
    country: str
    agreementRef: str
    leadName: str
    leadEmail: str
    leadTitle: str
    academicRecognitionRules: str = "Pending — partnership team to finalize credit mapping with faculty."


@router.post("/api/superadmin/partnerships/universities", status_code=201)
async def create_university(
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _CreateUniversityBody = Body(...),
):
    """Create a new university MOU partner."""
    uni_id = _slugify(body.name, "u")
    lead = {"name": body.leadName.strip(), "email": body.leadEmail.strip().lower(), "title": body.leadTitle.strip()}
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO partnership_universities
                (id, name, country, agreement_ref, agreement_signed_at,
                 students_in_flight, students_alumni, lead_contact,
                 supervisors, academic_recognition_rules, cohort)
            VALUES (%s,%s,%s,%s,%s,0,0,%s,'[]',%s,'[]')
            RETURNING *
            """,
            (
                uni_id, body.name.strip(), body.country.strip(),
                body.agreementRef.strip(), _now_iso(),
                json.dumps(lead), body.academicRecognitionRules.strip(),
            ),
        )
        row = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="create_university",
            target=body.name, target_id=uni_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return _uni_out(dict(row))


# ── PATCH /api/superadmin/partnerships/universities/{uni_id} ─────────────────

class _PatchUniversityBody(BaseModel):
    leadContact: dict | None = None
    supervisors: list[dict] | None = None
    academicRecognitionRules: str | None = None
    studentsInFlight: int | None = None


@router.patch("/api/superadmin/partnerships/universities/{uni_id}")
async def patch_university(
    uni_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _PatchUniversityBody = Body(...),
):
    """Update mutable fields on a university MOU."""
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM partnership_universities WHERE id = %s", (uni_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail={"error": "university_not_found", "uniId": uni_id})

    sets: list[str] = ["updated_at = NOW()"]
    params: list[Any] = []

    if body.leadContact is not None:
        sets.append("lead_contact = %s")
        params.append(json.dumps(body.leadContact))
    if body.supervisors is not None:
        sets.append("supervisors = %s")
        params.append(json.dumps(body.supervisors))
    if body.academicRecognitionRules is not None:
        sets.append("academic_recognition_rules = %s")
        params.append(body.academicRecognitionRules)
    if body.studentsInFlight is not None:
        sets.append("students_in_flight = %s")
        params.append(body.studentsInFlight)

    params.append(uni_id)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"UPDATE partnership_universities SET {', '.join(sets)} WHERE id = %s RETURNING *",
            tuple(params),
        )
        row = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="update_university",
            target=uni_id, target_id=uni_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return _uni_out(dict(row))


# ── POST /api/superadmin/partnerships/universities/{uni_id}/students ──────────

class _AddStudentBody(BaseModel):
    name: str
    email: str
    rollNumber: str | None = None
    programme: str | None = None
    supervisorEmail: str | None = None
    status: str = "invited"


@router.post("/api/superadmin/partnerships/universities/{uni_id}/students", status_code=201)
async def add_university_student(
    uni_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _AddStudentBody = Body(...),
):
    """Add a student to the university cohort array."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM partnership_universities WHERE id = %s", (uni_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "university_not_found", "uniId": uni_id})

    uni = dict(row)
    cohort: list[dict] = _load_jsonb(uni["cohort"]) or []

    email = body.email.strip().lower()
    if any(s["email"] == email for s in cohort):
        raise HTTPException(status_code=409, detail={"error": "student_exists", "email": email})

    new_student: dict[str, Any] = {
        "id": f"stu-{int(time.time() * 1000)}",
        "name": body.name.strip(),
        "email": email,
        "status": body.status,
        "enrolledAt": _now_iso(),
        "inviteToken": _mint_token("uni"),
    }
    if body.rollNumber:
        new_student["rollNumber"] = body.rollNumber.strip()
    if body.programme:
        new_student["programme"] = body.programme.strip()
    if body.supervisorEmail:
        new_student["supervisorEmail"] = body.supervisorEmail.strip()

    cohort.append(new_student)
    in_flight_statuses = {"registered", "onboarding", "active"}
    in_flight = sum(1 for s in cohort if s.get("status") in in_flight_statuses)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "UPDATE partnership_universities SET cohort = %s, students_in_flight = %s, updated_at = NOW() WHERE id = %s RETURNING *",
            (json.dumps(cohort), in_flight, uni_id),
        )
        updated = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="add_university_student",
            target=email, target_id=uni_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return {"student": new_student, "university": _uni_out(dict(updated))}


# ── POST /api/superadmin/partnerships/universities/{uni_id}/students/{sid}/invite-sent

@router.post("/api/superadmin/partnerships/universities/{uni_id}/students/{sid}/invite-sent")
async def university_student_invite_sent(
    uni_id: str,
    sid: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Mark a student's invite as sent (sets inviteSentAt timestamp)."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM partnership_universities WHERE id = %s", (uni_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "university_not_found", "uniId": uni_id})

    uni = dict(row)
    cohort: list[dict] = _load_jsonb(uni["cohort"]) or []

    idx = next((i for i, s in enumerate(cohort) if s["id"] == sid), None)
    if idx is None:
        raise HTTPException(status_code=404, detail={"error": "student_not_found", "sid": sid})

    cohort[idx] = {**cohort[idx], "inviteSentAt": _now_iso()}

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "UPDATE partnership_universities SET cohort = %s, updated_at = NOW() WHERE id = %s RETURNING *",
            (json.dumps(cohort), uni_id),
        )
        updated = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="university_student_invite_sent",
            target=sid, target_id=uni_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return {"student": cohort[idx], "university": _uni_out(dict(updated))}


# ── POST /api/superadmin/partnerships/universities/{uni_id}/supervisors ───────

class _AddSupervisorBody(BaseModel):
    name: str
    email: str
    department: str = ""


@router.post("/api/superadmin/partnerships/universities/{uni_id}/supervisors", status_code=201)
async def add_university_supervisor(
    uni_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _AddSupervisorBody = Body(...),
):
    """Append a supervisor to the university's supervisors array."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM partnership_universities WHERE id = %s", (uni_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"error": "university_not_found", "uniId": uni_id})

    uni = dict(row)
    supervisors: list[dict] = _load_jsonb(uni["supervisors"]) or []

    email = body.email.strip().lower()
    if any(s["email"] == email for s in supervisors):
        raise HTTPException(status_code=409, detail={"error": "supervisor_exists", "email": email})

    new_sup = {"name": body.name.strip(), "email": email, "department": body.department.strip()}
    supervisors.append(new_sup)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "UPDATE partnership_universities SET supervisors = %s, updated_at = NOW() WHERE id = %s RETURNING *",
            (json.dumps(supervisors), uni_id),
        )
        updated = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="add_university_supervisor",
            target=email, target_id=uni_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return {"supervisor": new_sup, "university": _uni_out(dict(updated))}
