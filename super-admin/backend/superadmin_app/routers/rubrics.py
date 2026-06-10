"""
Rubric template library endpoints for the Super Admin portal.

Spec: MockRubricTemplate / MockRubricCriterion / MockFeedbackSnippet
      from newfrontend/frontend/src/mocks/admin/rubrics.ts  (spec doc 04 §5.F)

Tables (created on first import, idempotent):
  rubric_templates   — main template rows; criteria stored as JSONB array
  rubric_feedback    — feedback snippets keyed to a template id

Endpoints:
  GET  /api/superadmin/rubric-templates           — list all templates
  GET  /api/superadmin/rubric-templates/{id}      — single template
  POST /api/superadmin/rubric-templates           — create (version starts at 1)
  PUT  /api/superadmin/rubric-templates/{id}      — update (bumps version)
  GET  /api/superadmin/rubric-templates/{id}/feedback  — list feedback snippets
  PUT  /api/superadmin/rubric-templates/{id}/feedback  — replace feedback snippets
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["superadmin-rubrics"])

# ── DB helpers ────────────────────────────────────────────────────────────────


def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


# ── Schema init ───────────────────────────────────────────────────────────────

_SCHEMA_INIT_SQL = """
CREATE TABLE IF NOT EXISTS rubric_templates (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    applies_to    TEXT NOT NULL,
    criteria      JSONB NOT NULL DEFAULT '[]',
    used_by_tenants INTEGER NOT NULL DEFAULT 0,
    version       INTEGER NOT NULL DEFAULT 1,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rubric_feedback (
    id              TEXT PRIMARY KEY,
    template_id     TEXT NOT NULL REFERENCES rubric_templates(id) ON DELETE CASCADE,
    criterion_label TEXT NOT NULL,
    score_range     TEXT NOT NULL,
    text            TEXT NOT NULL
);
"""

# Seed data mirroring MOCK_RUBRIC_TEMPLATES + MOCK_RUBRIC_FEEDBACK exactly.
_SEED_TEMPLATES = [
    {
        "id": "rt-sw",
        "name": "Software default",
        "applies_to": "Code",
        "criteria": json.dumps([
            {"id": "c1", "label": "Correctness",    "description": "Code produces expected output for all documented inputs.",          "weight": 25, "scaleMax": 5},
            {"id": "c2", "label": "Tests",           "description": "Unit + integration coverage for new logic.",                        "weight": 20, "scaleMax": 5},
            {"id": "c3", "label": "Readability",     "description": "Naming, structure, and documentation aid future readers.",           "weight": 15, "scaleMax": 5},
            {"id": "c4", "label": "Idiomatic use",   "description": "Solution fits the framework / language conventions.",               "weight": 10, "scaleMax": 5},
            {"id": "c5", "label": "Security",        "description": "No new security regressions; secrets handled correctly.",            "weight": 10, "scaleMax": 5},
            {"id": "c6", "label": "Performance",     "description": "Avoids needless work; meets stated latency target.",                "weight": 10, "scaleMax": 5},
            {"id": "c7", "label": "Error handling",  "description": "Failures surface usefully; boundaries respected.",                  "weight":  5, "scaleMax": 5},
            {"id": "c8", "label": "Spec adherence",  "description": "Matches the brief's checklist + acceptance criteria.",              "weight":  5, "scaleMax": 5},
        ]),
        "used_by_tenants": 12,
        "version": 4,
        "updated_at": "2026-05-12T10:00:00+00:00",
    },
    {
        "id": "rt-design",
        "name": "Design default",
        "applies_to": "Design",
        "criteria": json.dumps([
            {"id": "c1", "label": "Brief fit",        "description": "Solution addresses the stated user + business goal.",  "weight": 25, "scaleMax": 5},
            {"id": "c2", "label": "Visual hierarchy", "description": "Information priority reads correctly.",                "weight": 20, "scaleMax": 5},
            {"id": "c3", "label": "Accessibility",    "description": "Contrast, focus, alt-text, motion preferences.",       "weight": 20, "scaleMax": 5},
            {"id": "c4", "label": "System fit",       "description": "Reuses tokens + components; doesn't fork the system.", "weight": 15, "scaleMax": 5},
            {"id": "c5", "label": "Craft",            "description": "Polish: spacing, alignment, typography.",               "weight": 10, "scaleMax": 5},
            {"id": "c6", "label": "Rationale",        "description": "Decisions are documented + defendable.",               "weight": 10, "scaleMax": 5},
        ]),
        "used_by_tenants": 14,
        "version": 3,
        "updated_at": "2026-04-18T14:00:00+00:00",
    },
    {
        "id": "rt-data",
        "name": "Data default",
        "applies_to": "Data",
        "criteria": json.dumps([
            {"id": "c1", "label": "Question fit",    "description": "Analysis answers the brief's actual question.",      "weight": 25, "scaleMax": 5},
            {"id": "c2", "label": "Method choice",   "description": "Selected technique is appropriate for the data.",    "weight": 20, "scaleMax": 5},
            {"id": "c3", "label": "Reproducibility", "description": "Notebook + data lineage allow re-run by reviewer.",  "weight": 15, "scaleMax": 5},
            {"id": "c4", "label": "Honest framing",  "description": "Limits, caveats, and confidence stated.",            "weight": 15, "scaleMax": 5},
            {"id": "c5", "label": "Visual clarity",  "description": "Charts read at a glance; no chartjunk.",             "weight": 10, "scaleMax": 5},
            {"id": "c6", "label": "Narrative",       "description": "Reader is led to the insight, not buried in numbers.", "weight": 10, "scaleMax": 5},
            {"id": "c7", "label": "Rigor",           "description": "Statistical / sampling assumptions checked.",        "weight":  5, "scaleMax": 5},
        ]),
        "used_by_tenants": 8,
        "version": 2,
        "updated_at": "2026-03-10T12:00:00+00:00",
    },
    {
        "id": "rt-mktg",
        "name": "Marketing default",
        "applies_to": "Marketing",
        "criteria": json.dumps([
            {"id": "c1", "label": "Audience fit",    "description": "Speaks to the segment in the brief.",   "weight": 30, "scaleMax": 5},
            {"id": "c2", "label": "Message clarity", "description": "Promise + reason-to-believe land fast.", "weight": 25, "scaleMax": 5},
            {"id": "c3", "label": "Brand voice",     "description": "Tone + vocabulary match the brand.",     "weight": 20, "scaleMax": 5},
            {"id": "c4", "label": "CTA",             "description": "Action + path is unambiguous.",          "weight": 15, "scaleMax": 5},
            {"id": "c5", "label": "Channel fit",     "description": "Format fits the medium it'll run in.",   "weight": 10, "scaleMax": 5},
        ]),
        "used_by_tenants": 6,
        "version": 2,
        "updated_at": "2026-04-02T10:00:00+00:00",
    },
    {
        "id": "rt-docs",
        "name": "Documentation default",
        "applies_to": "Documentation",
        "criteria": json.dumps([
            {"id": "c1", "label": "Audience fit",    "description": "Right level for the stated reader.",            "weight": 25, "scaleMax": 5},
            {"id": "c2", "label": "Completeness",    "description": "Covers all required journeys / surfaces.",       "weight": 25, "scaleMax": 5},
            {"id": "c3", "label": "Accuracy",        "description": "Facts + examples verifiable.",                   "weight": 20, "scaleMax": 5},
            {"id": "c4", "label": "Findability",     "description": "TOC + headings + cross-links aid scanning.",     "weight": 15, "scaleMax": 5},
            {"id": "c5", "label": "Maintainability", "description": "Written so future updates are cheap.",           "weight": 15, "scaleMax": 5},
        ]),
        "used_by_tenants": 9,
        "version": 3,
        "updated_at": "2026-05-04T11:00:00+00:00",
    },
]

_SEED_FEEDBACK = [
    # rt-sw
    {"id": "rt-sw-fb1", "template_id": "rt-sw", "criterion_label": "Correctness", "score_range": "1–2",
     "text": "Several paths fail on edge cases documented in the brief. Re-run against the acceptance checklist."},
    {"id": "rt-sw-fb2", "template_id": "rt-sw", "criterion_label": "Correctness", "score_range": "4–5",
     "text": "Output matches spec for all documented inputs; behaviour is predictable."},
    {"id": "rt-sw-fb3", "template_id": "rt-sw", "criterion_label": "Tests", "score_range": "1–2",
     "text": "Coverage is thin on new logic; add cases for failure modes."},
    # rt-design
    {"id": "rt-design-fb1", "template_id": "rt-design", "criterion_label": "Brief fit", "score_range": "1–2",
     "text": "Core user goal from the brief is not clearly addressed in the primary flow."},
    {"id": "rt-design-fb2", "template_id": "rt-design", "criterion_label": "Accessibility", "score_range": "3",
     "text": "Contrast passes on main screens; verify focus order on modals."},
    # rt-data
    {"id": "rt-data-fb1", "template_id": "rt-data", "criterion_label": "Question fit", "score_range": "1–2",
     "text": "Analysis answers an adjacent question — realign to the stated hypothesis."},
    # rt-mktg
    {"id": "rt-mktg-fb1", "template_id": "rt-mktg", "criterion_label": "Audience fit", "score_range": "4–5",
     "text": "Tone and examples match the segment defined in the brief."},
    # rt-docs
    {"id": "rt-docs-fb1", "template_id": "rt-docs", "criterion_label": "Completeness", "score_range": "1–2",
     "text": "Required setup steps and error paths are missing from the guide."},
]


def init_rubrics_schema() -> None:
    """Create tables and seed mock data idempotently.  Called once at startup."""
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_INIT_SQL)
            for t in _SEED_TEMPLATES:
                cur.execute(
                    """
                    INSERT INTO rubric_templates
                        (id, name, applies_to, criteria, used_by_tenants, version, updated_at)
                    VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        t["id"], t["name"], t["applies_to"],
                        t["criteria"],
                        t["used_by_tenants"], t["version"], t["updated_at"],
                    ),
                )
            for fb in _SEED_FEEDBACK:
                cur.execute(
                    """
                    INSERT INTO rubric_feedback
                        (id, template_id, criterion_label, score_range, text)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (fb["id"], fb["template_id"], fb["criterion_label"],
                     fb["score_range"], fb["text"]),
                )
        conn.commit()
        logger.info("rubric_templates + rubric_feedback schema/seed OK")
    except Exception as exc:  # noqa: BLE001
        logger.warning("rubrics schema init failed (non-fatal): %s", exc)
        try:
            get_pg_connection().rollback()
        except Exception:
            pass


# Run on module load so the tables exist before any request arrives.
try:
    init_rubrics_schema()
except Exception:  # noqa: BLE001
    pass


# ── Serialisation helper ──────────────────────────────────────────────────────

def _row_to_template(row: dict[str, Any]) -> dict[str, Any]:
    """Map a rubric_templates DB row to the MockRubricTemplate FE shape."""
    criteria = row.get("criteria") or []
    if isinstance(criteria, str):
        try:
            criteria = json.loads(criteria)
        except (ValueError, TypeError):
            criteria = []
    return {
        "id": row["id"],
        "name": row["name"],
        "appliesTo": row["applies_to"],
        "criteria": criteria,
        "usedByTenants": row.get("used_by_tenants") or 0,
        "version": row.get("version") or 1,
        "updatedAt": _iso(row.get("updated_at")),
    }


def _row_to_feedback(row: dict[str, Any]) -> dict[str, Any]:
    """Map a rubric_feedback DB row to the MockFeedbackSnippet FE shape."""
    return {
        "id": row["id"],
        "criterionLabel": row["criterion_label"],
        "scoreRange": row["score_range"],
        "text": row["text"],
    }


# ── Pydantic models ───────────────────────────────────────────────────────────

class _CriterionIn(BaseModel):
    id: str | None = None
    label: str
    description: str
    weight: int
    scaleMax: int = 5


class _RubricCreateBody(BaseModel):
    name: str
    appliesTo: str
    criteria: list[_CriterionIn] = []
    usedByTenants: int = 0


class _RubricUpdateBody(BaseModel):
    name: str | None = None
    appliesTo: str | None = None
    criteria: list[_CriterionIn] | None = None
    usedByTenants: int | None = None


class _FeedbackSnippetIn(BaseModel):
    id: str | None = None
    criterionLabel: str
    scoreRange: str
    text: str


class _FeedbackUpdateBody(BaseModel):
    snippets: list[_FeedbackSnippetIn]


# ── GET /api/superadmin/rubric-templates ─────────────────────────────────────

@router.get("/api/superadmin/rubric-templates")
async def list_rubric_templates(
    admin: Annotated[dict, Depends(get_current_admin)],
    applies_to: str | None = None,
    q: str | None = None,
):
    """Return all rubric templates.  Optional filters: appliesTo, q (name search)."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, name, applies_to, criteria, used_by_tenants, version, updated_at "
            "FROM rubric_templates ORDER BY name ASC"
        )
        rows = list(cur.fetchall())

    items = [_row_to_template(r) for r in rows]

    if applies_to:
        items = [i for i in items if i["appliesTo"].lower() == applies_to.lower()]
    if q:
        needle = q.strip().lower()
        items = [i for i in items if needle in i["name"].lower()]

    return items


# ── GET /api/superadmin/rubric-templates/{template_id} ───────────────────────

@router.get("/api/superadmin/rubric-templates/{template_id}")
async def get_rubric_template(
    template_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Return a single rubric template by id."""
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, name, applies_to, criteria, used_by_tenants, version, updated_at "
            "FROM rubric_templates WHERE id = %s",
            (template_id,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail={"error": "rubric_not_found", "id": template_id})

    return _row_to_template(row)


# ── POST /api/superadmin/rubric-templates ────────────────────────────────────

_VALID_APPLIES_TO = {"Code", "Design", "Data", "Marketing", "Documentation"}


@router.post("/api/superadmin/rubric-templates", status_code=201)
async def create_rubric_template(
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _RubricCreateBody = Body(...),
):
    """Create a new rubric template.  Version starts at 1."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    applies_to = body.appliesTo.strip() if body.appliesTo else ""
    if applies_to not in _VALID_APPLIES_TO:
        raise HTTPException(
            status_code=422,
            detail=f"appliesTo must be one of: {', '.join(sorted(_VALID_APPLIES_TO))}",
        )

    new_id = f"rt-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    # Ensure each criterion has an id.
    criteria_out = []
    for i, c in enumerate(body.criteria):
        criteria_out.append({
            "id": c.id or f"c{i + 1}",
            "label": c.label,
            "description": c.description,
            "weight": c.weight,
            "scaleMax": c.scaleMax,
        })

    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO rubric_templates
                (id, name, applies_to, criteria, used_by_tenants, version, updated_at, created_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, 1, %s, %s)
            RETURNING id, name, applies_to, criteria, used_by_tenants, version, updated_at
            """,
            (new_id, name, applies_to, json.dumps(criteria_out),
             body.usedByTenants, now, now),
        )
        row = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="create_rubric_template",
            target=name, target_id=new_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return _row_to_template(row)


# ── PUT /api/superadmin/rubric-templates/{template_id} ───────────────────────

@router.put("/api/superadmin/rubric-templates/{template_id}")
async def update_rubric_template(
    template_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _RubricUpdateBody = Body(...),
):
    """Update fields on a rubric template and bump its version."""
    conn = _conn()

    # Fetch current row.
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, name, applies_to, criteria, used_by_tenants, version "
            "FROM rubric_templates WHERE id = %s",
            (template_id,),
        )
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail={"error": "rubric_not_found", "id": template_id})

    # Merge updates.
    new_name = (body.name or "").strip() or row["name"]
    new_applies = (body.appliesTo or "").strip() or row["applies_to"]
    if body.appliesTo and new_applies not in _VALID_APPLIES_TO:
        raise HTTPException(
            status_code=422,
            detail=f"appliesTo must be one of: {', '.join(sorted(_VALID_APPLIES_TO))}",
        )

    if body.criteria is not None:
        existing = row.get("criteria") or []
        if isinstance(existing, str):
            try:
                existing = json.loads(existing)
            except (ValueError, TypeError):
                existing = []
        criteria_out = []
        for i, c in enumerate(body.criteria):
            criteria_out.append({
                "id": c.id or f"c{i + 1}",
                "label": c.label,
                "description": c.description,
                "weight": c.weight,
                "scaleMax": c.scaleMax,
            })
        new_criteria = json.dumps(criteria_out)
    else:
        existing = row.get("criteria") or []
        new_criteria = json.dumps(existing) if isinstance(existing, list) else (existing or "[]")

    new_used = body.usedByTenants if body.usedByTenants is not None else (row.get("used_by_tenants") or 0)
    new_version = (row.get("version") or 1) + 1
    now = datetime.now(timezone.utc)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            UPDATE rubric_templates
               SET name = %s,
                   applies_to = %s,
                   criteria = %s::jsonb,
                   used_by_tenants = %s,
                   version = %s,
                   updated_at = %s
             WHERE id = %s
            RETURNING id, name, applies_to, criteria, used_by_tenants, version, updated_at
            """,
            (new_name, new_applies, new_criteria, new_used, new_version, now, template_id),
        )
        updated = cur.fetchone()
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="update_rubric_template",
            target=new_name, target_id=template_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return _row_to_template(updated)


# ── GET /api/superadmin/rubric-templates/{template_id}/feedback ──────────────

@router.get("/api/superadmin/rubric-templates/{template_id}/feedback")
async def get_rubric_feedback(
    template_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """Return all feedback snippets for a rubric template."""
    conn = _conn()

    # Confirm template exists.
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM rubric_templates WHERE id = %s", (template_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail={"error": "rubric_not_found", "id": template_id})

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, criterion_label, score_range, text "
            "FROM rubric_feedback WHERE template_id = %s "
            "ORDER BY criterion_label, score_range",
            (template_id,),
        )
        rows = list(cur.fetchall())

    return [_row_to_feedback(r) for r in rows]


# ── PUT /api/superadmin/rubric-templates/{template_id}/feedback ──────────────

@router.put("/api/superadmin/rubric-templates/{template_id}/feedback")
async def replace_rubric_feedback(
    template_id: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    body: _FeedbackUpdateBody = Body(...),
):
    """Replace all feedback snippets for a rubric template (full replace)."""
    conn = _conn()

    # Confirm template exists.
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM rubric_templates WHERE id = %s", (template_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail={"error": "rubric_not_found", "id": template_id})

    with conn.cursor() as cur:
        cur.execute("DELETE FROM rubric_feedback WHERE template_id = %s", (template_id,))
        result_rows = []
        for snippet in body.snippets:
            new_id = snippet.id or f"fb-{uuid.uuid4().hex[:8]}"
            cur.execute(
                """
                INSERT INTO rubric_feedback (id, template_id, criterion_label, score_range, text)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (new_id, template_id, snippet.criterionLabel, snippet.scoreRange, snippet.text),
            )
            result_rows.append({
                "id": new_id,
                "criterionLabel": snippet.criterionLabel,
                "scoreRange": snippet.scoreRange,
                "text": snippet.text,
            })
    conn.commit()

    try:
        write_audit(
            actor_id=admin.get("id"), actor_email=admin.get("email"),
            actor_role=admin.get("role"), action="update_rubric_feedback",
            target="rubric_feedback", target_id=template_id,
        )
    except Exception:  # noqa: BLE001
        pass

    return result_rows
