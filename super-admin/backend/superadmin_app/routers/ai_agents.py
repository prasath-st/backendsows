"""
AI Agents + Matching module for the superadmin service.

Endpoints:
  GET  /api/admin/agents/{agentId}/prompts/{templateName}/versions
  POST /api/admin/agents/{agentId}/prompts/{templateName}/rollback
  POST /api/ai/invoke
  GET  /api/v1/matching/tasks/{taskId}/candidates
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from shared.audit import write_audit
from shared.db import ensure_pg_clean, get_pg_connection
from shared.deps import get_current_admin, get_current_user
from psycopg2.extras import Json, RealDictCursor

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ai-agents-matching"])

# ── Postgres table init (called from init_superadmin_schema) ─────────────────
# Kept here so everything about this module stays together.

AI_AGENTS_SCHEMA_SQL = """
-- Agent catalogue: one row per logical agent
CREATE TABLE IF NOT EXISTS agents (
    id               TEXT PRIMARY KEY,
    model_id         TEXT NOT NULL DEFAULT 'glimmora-mock-v1',
    is_enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    active_prompt_id TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Template: the named slot within an agent (e.g. 'score-rubric')
CREATE TABLE IF NOT EXISTS agent_prompt_templates (
    id         TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    UNIQUE (agent_id, name)
);

-- Immutable prompt versions
CREATE TABLE IF NOT EXISTS agent_prompt_versions (
    id                  TEXT PRIMARY KEY,
    prompt_template_id  TEXT NOT NULL REFERENCES agent_prompt_templates(id) ON DELETE CASCADE,
    version             INT NOT NULL,
    body                TEXT NOT NULL,
    variables           TEXT[] NOT NULL DEFAULT '{}',
    notes               TEXT,
    created_by          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (prompt_template_id, version)
);
CREATE INDEX IF NOT EXISTS idx_apv_template ON agent_prompt_versions(prompt_template_id);

-- Back-fill the FK so agents.active_prompt_id can point at a version
-- (Postgres can't forward-reference, so we add it after the versions table).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_agents_active_prompt'
          AND table_name = 'agents'
    ) THEN
        ALTER TABLE agents
            ADD CONSTRAINT fk_agents_active_prompt
            FOREIGN KEY (active_prompt_id)
            REFERENCES agent_prompt_versions(id)
            ON DELETE SET NULL
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END$$;

-- Invocations log (idempotency key + audit trail)
CREATE TABLE IF NOT EXISTS agent_invocations (
    id                TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    agent_id          TEXT NOT NULL,
    prompt_version_id TEXT,
    model_id          TEXT NOT NULL,
    tenant_id         TEXT,
    actor_user_id     TEXT NOT NULL,
    request_id        TEXT UNIQUE,
    input             JSONB NOT NULL DEFAULT '{}',
    output            JSONB,
    confidence        NUMERIC,
    latency_ms        INT,
    status            TEXT NOT NULL DEFAULT 'success',
    error_message     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_invocations_agent ON agent_invocations(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_invocations_request ON agent_invocations(request_id) WHERE request_id IS NOT NULL;

-- Seed canonical agents if they don't exist yet
INSERT INTO agents (id, model_id, is_enabled)
VALUES
    ('sow-intake',          'glimmora-mock-v1', TRUE),
    ('decomposition',       'glimmora-mock-v1', TRUE),
    ('contributor-support', 'glimmora-mock-v1', TRUE),
    ('review-assistant',    'glimmora-mock-v1', TRUE)
ON CONFLICT (id) DO NOTHING;
"""


def init_ai_agents_schema() -> None:
    """Idempotent schema creation. Called from app.py startup."""
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(AI_AGENTS_SCHEMA_SQL)
    conn.commit()
    logger.info("AI-agents schema ensured.")


# ── Internal helpers ─────────────────────────────────────────────────────────

def _conn():
    ensure_pg_clean()
    return get_pg_connection()


def _ensure_template(conn, agent_id: str, template_name: str) -> str:
    """Return template id, creating the template row if missing."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id FROM agent_prompt_templates WHERE agent_id = %s AND name = %s",
            (agent_id, template_name),
        )
        row = cur.fetchone()
    if row:
        return row["id"]
    template_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_prompt_templates (id, agent_id, name) VALUES (%s,%s,%s) "
            "ON CONFLICT (agent_id, name) DO NOTHING",
            (template_id, agent_id, template_name),
        )
    conn.commit()
    # Re-fetch in case of a concurrent insert
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id FROM agent_prompt_templates WHERE agent_id = %s AND name = %s",
            (agent_id, template_name),
        )
        return cur.fetchone()["id"]


def _version_out(row: dict, template_name: str, agent_id: str, active_prompt_id: str | None) -> dict:
    variables = row.get("variables") or []
    if isinstance(variables, str):
        try:
            variables = json.loads(variables)
        except (ValueError, TypeError):
            variables = []
    is_active = row["id"] == active_prompt_id
    created_iso = row["created_at"].isoformat() if row.get("created_at") else None
    return {
        "id": row["id"],
        "promptTemplateId": row["prompt_template_id"],
        "templateName": template_name,
        "agentId": agent_id,
        "version": row["version"],
        "body": row.get("body", ""),
        "variables": variables,
        # FE PromptVersion field names:
        "status": "active" if is_active else "inactive",
        "activatedAt": created_iso if is_active else None,
        "author": row.get("created_by"),
        "changelog": row.get("notes"),
        "expectedSchema": row.get("expected_schema") or "",
        # Back-compat keys:
        "notes": row.get("notes"),
        "createdBy": row.get("created_by"),
        "createdAt": created_iso,
        "isActive": is_active,
    }


# ── Static metadata for the 4 MVP agents ─────────────────────────────────────
# The agents table only stores runtime config (model_id, is_enabled, active_prompt_id).
# Display-only fields (name, description, portal) are maintained here alongside the
# DB-seeded ids so the frontend ai-agents-workspace gets the full shape it expects.

_AGENT_META: dict[str, dict] = {
    "sow-intake": {
        "name": "SOW Intake Assistant",
        "shortName": "SOW Intake",
        "description": "Normalizes uploaded SOW documents and surfaces risk + compliance flags.",
        "portal": "enterprise",
    },
    "decomposition": {
        "name": "Decomposition Assistant",
        "shortName": "Decomposition",
        "description": "Suggests milestone + task breakdown from approved SOWs.",
        "portal": "enterprise",
    },
    "contributor-support": {
        "name": "Contributor Support Assistant",
        "shortName": "Contributor Support",
        "description": "Helps contributors triage their own questions before opening a ticket.",
        "portal": "contributor",
    },
    "review-assistant": {
        "name": "Review Assistant",
        "shortName": "Review",
        "description": "Drafts rubric-aligned score suggestions for mentor review.",
        "portal": "mentor",
    },
}


# ── GET /api/admin/agents ─────────────────────────────────────────────────────

@router.get("/api/admin/agents")
async def list_agents(
    admin: Annotated[dict, Depends(get_current_admin)],
):
    """
    Return all registered agents enriched with 24h invocation telemetry.

    Response shape (array item):
      id, name, shortName, description, portal,
      status ("enabled"|"paused"), modelId, activePromptId, activePromptVersion,
      recentInvocations24h, avgLatencyMs, errors24h
    """
    conn = _conn()

    # Fetch all agent rows
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, model_id, is_enabled, active_prompt_id, created_at, updated_at "
            "FROM agents ORDER BY id"
        )
        agent_rows = cur.fetchall()

    # Fetch 24h telemetry per agent in one query
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT agent_id,
                   COUNT(*)                                             AS total,
                   COUNT(*) FILTER (WHERE status != 'success')         AS errors,
                   COALESCE(AVG(latency_ms) FILTER (WHERE latency_ms IS NOT NULL), 0) AS avg_latency_ms
              FROM agent_invocations
             WHERE created_at >= now() - INTERVAL '24 hours'
             GROUP BY agent_id
            """
        )
        tele_rows = cur.fetchall()

    telemetry: dict[str, dict] = {
        r["agent_id"]: {
            "recentInvocations24h": int(r["total"]),
            "errors24h": int(r["errors"]),
            "avgLatencyMs": int(round(float(r["avg_latency_ms"]))),
        }
        for r in tele_rows
    }

    # Resolve active prompt version number for each agent
    active_ids = [
        r["active_prompt_id"] for r in agent_rows if r.get("active_prompt_id")
    ]
    version_map: dict[str, int] = {}
    if active_ids:
        placeholders = ", ".join(["%s"] * len(active_ids))
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT id, version FROM agent_prompt_versions WHERE id IN ({placeholders})",
                active_ids,
            )
            for vrow in cur.fetchall():
                version_map[vrow["id"]] = vrow["version"]

    items = []
    for row in agent_rows:
        agent_id = row["id"]
        meta = _AGENT_META.get(agent_id, {
            "name": agent_id,
            "shortName": agent_id,
            "description": "",
            "portal": "all",
        })
        tele = telemetry.get(agent_id, {
            "recentInvocations24h": 0,
            "errors24h": 0,
            "avgLatencyMs": 0,
        })
        active_prompt_id = row.get("active_prompt_id")
        items.append({
            "id": agent_id,
            "name": meta["name"],
            "shortName": meta["shortName"],
            "description": meta["description"],
            "portal": meta["portal"],
            "status": "enabled" if row.get("is_enabled") else "paused",
            "modelId": row.get("model_id") or "glimmora-mock-v1",
            "activePromptId": active_prompt_id,
            "activePromptVersion": version_map.get(active_prompt_id) if active_prompt_id else None,
            "recentInvocations24h": tele["recentInvocations24h"],
            "avgLatencyMs": tele["avgLatencyMs"],
            "errors24h": tele["errors24h"],
            "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
            "updatedAt": row["updated_at"].isoformat() if row.get("updated_at") else None,
        })

    return {"items": items}


# ── GET /api/admin/agents/{agentId}/prompts/{templateName}/versions ──────────

@router.get("/api/admin/agents/{agentId}/prompts/{templateName}/versions")
async def list_prompt_versions(
    agentId: str,
    templateName: str,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    conn = _conn()
    # Check agent exists
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, active_prompt_id FROM agents WHERE id = %s", (agentId,))
        agent = cur.fetchone()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check template
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id FROM agent_prompt_templates WHERE agent_id = %s AND name = %s",
            (agentId, templateName),
        )
        template = cur.fetchone()
    if not template:
        raise HTTPException(status_code=404, detail="Prompt template not found")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM agent_prompt_versions WHERE prompt_template_id = %s ORDER BY version DESC",
            (template["id"],),
        )
        rows = cur.fetchall()

    versions = [_version_out(r, templateName, agentId, agent.get("active_prompt_id")) for r in rows]
    return {"versions": versions}


# ── POST /api/admin/agents/{agentId}/prompts/{templateName}/rollback ─────────

class RollbackRequest(BaseModel):
    targetVersionId: str
    reason: str | None = None


@router.post("/api/admin/agents/{agentId}/prompts/{templateName}/rollback")
async def rollback_prompt_version(
    agentId: str,
    templateName: str,
    body: RollbackRequest,
    request: Request,
    admin: Annotated[dict, Depends(get_current_admin)],
):
    conn = _conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, active_prompt_id FROM agents WHERE id = %s", (agentId,))
        agent = cur.fetchone()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Validate target version exists and belongs to this agent
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT pv.id, pv.version, pt.name AS template_name, pt.agent_id
              FROM agent_prompt_versions pv
              JOIN agent_prompt_templates pt ON pt.id = pv.prompt_template_id
             WHERE pv.id = %s
            """,
            (body.targetVersionId,),
        )
        target = cur.fetchone()

    if not target:
        raise HTTPException(status_code=404, detail="Target prompt version not found")
    if target["agent_id"] != agentId:
        raise HTTPException(status_code=400, detail="Target prompt version does not belong to this agent")

    # Resolve from-version
    from_version_id = agent.get("active_prompt_id")
    from_version_num = None
    if from_version_id:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT version FROM agent_prompt_versions WHERE id = %s", (from_version_id,))
            fv = cur.fetchone()
        if fv:
            from_version_num = fv["version"]

    # Flip the pointer
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE agents SET active_prompt_id = %s, updated_at = now() WHERE id = %s",
            (body.targetVersionId, agentId),
        )
    conn.commit()

    write_audit(
        actor_id=str(admin.get("id", "")),
        actor_email=admin.get("email"),
        actor_role=admin.get("role"),
        action="ai.prompt.rollback",
        target="agent_prompt_version",
        target_id=body.targetVersionId,
        service="superadmin-service",
        ip_address=request.client.host if request.client else None,
        extra={
            "agentId": agentId,
            "templateName": templateName,
            "fromPromptVersionId": from_version_id,
            "fromVersion": from_version_num,
            "toPromptVersionId": body.targetVersionId,
            "toVersion": target["version"],
            "reason": body.reason,
        },
    )

    return {
        "result": {
            "agentId": agentId,
            "fromPromptVersionId": from_version_id,
            "fromVersion": from_version_num,
            "toPromptVersionId": body.targetVersionId,
            "toVersion": target["version"],
            "templateName": target["template_name"],
        }
    }


# ── POST /api/ai/invoke ───────────────────────────────────────────────────────

_VALID_AGENT_IDS = {"sow-intake", "decomposition", "contributor-support", "review-assistant"}

_AGENT_ALLOWED_ROLES: dict[str, set[str]] = {
    "sow-intake": {"enterprise", "enterprise_admin", "admin", "superadmin", "super_admin"},
    "decomposition": {"enterprise", "enterprise_admin", "admin", "superadmin", "super_admin"},
    "contributor-support": {"contributor", "mentor", "admin", "superadmin", "super_admin", "enterprise"},
    "review-assistant": {"mentor", "reviewer", "admin", "superadmin", "super_admin"},
}


class AgentInvokeRequest(BaseModel):
    agentId: str
    promptName: str
    variables: dict[str, Any]
    requestId: str | None = None


def _simulate_agent(agent_id: str, variables: dict[str, Any]) -> dict[str, Any]:
    """Deterministic Phase-1 simulation — mirrors the FE mock handlers."""
    if agent_id == "sow-intake":
        doc_len = len(variables.get("sowDocText") or "")
        completeness = min(100, round((doc_len / 1000) * 20))
        confidence = 0.78 if doc_len > 500 else 0.42
        overall = "low" if completeness >= 80 else ("medium" if completeness >= 50 else "high")
        output = {
            "title": "Extracted SOW · placeholder title",
            "dates": {"start": None, "end": None},
            "sponsor": None,
            "stakeholders": [],
            "deliverables": [
                {"id": "d1", "title": "Placeholder deliverable 1"},
                {"id": "d2", "title": "Placeholder deliverable 2"},
            ],
            "clauses": {"dependencies": [], "assumptions": [], "constraints": []},
            "riskScore": {"completeness": completeness, "confidence": confidence, "overall": overall},
            "hallucinationFlags": ["very_short_input"] if doc_len < 200 else [],
        }
        sources = [{"kind": "task_field", "reference": "sowDocText",
                    "excerpt": (variables.get("sowDocText") or "")[:80]}]
        coverage_gaps = ["short_document_low_signal"] if doc_len < 500 else []
        risk_flags = ["insufficient_input"] if doc_len < 200 else []
        return {"output": output, "confidence": confidence, "sources": sources,
                "coverageGaps": coverage_gaps, "riskFlags": risk_flags}

    if agent_id == "decomposition":
        summary = variables.get("sowSummary") or ""
        summary_len = len(summary)
        confidence = 0.72 if summary_len > 500 else 0.5
        output = {
            "milestones": [
                {
                    "name": "Foundation",
                    "tasks": [
                        {"title": "Set up project scaffolding", "brief": "Initialize repo, CI, environments",
                         "skillTags": ["devops"], "level": "L2", "estimateHours": 8, "dependsOn": []},
                        {"title": "Define acceptance criteria",
                         "brief": "Translate SOW deliverables into testable criteria",
                         "skillTags": ["pm", "qa"], "level": "L3", "estimateHours": 12, "dependsOn": []},
                    ],
                },
                {
                    "name": "Build",
                    "tasks": [
                        {"title": "Implement primary feature", "brief": "Per SOW deliverable D1",
                         "skillTags": ["engineering"], "level": "L3", "estimateHours": 24,
                         "dependsOn": ["Set up project scaffolding"]},
                    ],
                },
            ],
            "missingTaskFlags": (
                ["sow_too_brief_review_manually", "no_security_review_task"]
                if summary_len < 300
                else ["no_security_review_task"]
            ),
        }
        sources = [{"kind": "task_field", "reference": "sowSummary"}]
        coverage_gaps = ["short_sow_summary"] if summary_len < 300 else ["sponsor_constraints_not_parsed"]
        return {"output": output, "confidence": confidence, "sources": sources,
                "coverageGaps": coverage_gaps, "riskFlags": []}

    if agent_id == "contributor-support":
        criteria = variables.get("criteria") or []
        evidence = variables.get("evidence") or []
        total_criteria = len(criteria)
        addressed = sum(1 for c in criteria if c.get("addressed"))
        remaining = total_criteria - addressed
        has_evidence = len(evidence) > 0
        signals = []
        if remaining > 0:
            plural = "on still" if remaining == 1 else "a still"
            signals.append(f"{remaining} criteri{plural} unaddressed — fastest path to acceptance.")
        if not has_evidence and variables.get("taskState") == "in_progress":
            signals.append("No evidence attached yet — reviewers expect at least one artifact per criterion.")
        if total_criteria > 0 and addressed / total_criteria >= 0.8:
            signals.append(f"Readiness {round((addressed / total_criteria) * 100)}% — close to submittable.")
        output = {"signals": signals[:3]}
        sources_list = [{"kind": "criterion", "reference": "criteria"}]
        if has_evidence:
            sources_list += [{"kind": "evidence_file", "reference": e["name"]}
                             for e in evidence[:3] if e.get("name")]
        return {"output": output, "confidence": 0.9, "sources": sources_list,
                "coverageGaps": [], "riskFlags": []}

    if agent_id == "review-assistant":
        criteria = variables.get("criteria") or []
        evidence = variables.get("evidence") or []
        has_evidence = len(evidence) > 0
        digital_twin = variables.get("contributorDigitalTwin") or {}
        acceptance_rate = digital_twin.get("acceptanceRate") or 0
        boost = min(0.15, (acceptance_rate - 0.7) * 0.5) if acceptance_rate else 0
        base_score = 4 if has_evidence else 3
        base_conf = 0.78 if has_evidence else 0.55
        suggestions = [
            {"criterionId": c.get("id"), "score": base_score,
             "confidence": min(0.95, base_conf + boost),
             "sources": [e["name"] for e in evidence[:2] if e.get("name")] if has_evidence else ["task_brief"]}
            for c in criteria
        ]
        overall = suggestions[0]["confidence"] if suggestions else 0.5
        sources_list = [{"kind": "task_field", "reference": "taskBrief"}]
        sources_list += [{"kind": "evidence_file", "reference": e["name"]}
                         for e in evidence[:3] if e.get("name")]
        coverage_gaps = [] if has_evidence else ["no_evidence_attached_scores_are_brief_only"]
        output = {"suggestions": suggestions}
        return {"output": output, "confidence": overall, "sources": sources_list,
                "coverageGaps": coverage_gaps, "riskFlags": []}

    # Fallback for unknown agents
    return {"output": {}, "confidence": 0.5, "sources": [], "coverageGaps": [], "riskFlags": []}


@router.post("/api/ai/invoke")
async def ai_invoke(
    body: AgentInvokeRequest,
    request: Request,
    user: Annotated[dict, Depends(get_current_user)],
):
    if body.agentId not in _VALID_AGENT_IDS:
        raise HTTPException(status_code=400, detail=f"Invalid agentId: {body.agentId}")

    # Role gate
    allowed_roles = _AGENT_ALLOWED_ROLES.get(body.agentId, set())
    user_role = (user.get("role") or "").lower()
    if user_role not in allowed_roles:
        return {"ok": False, "failure": {
            "requestId": body.requestId or str(uuid.uuid4()),
            "agentId": body.agentId,
            "status": "rejected",
            "reason": "agent_role_not_allowed",
            "latencyMs": 0,
        }}

    conn = _conn()
    request_id = body.requestId or str(uuid.uuid4())
    started_at = time.monotonic()

    # Idempotency: return cached result within 24h if same requestId
    if body.requestId:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM agent_invocations WHERE request_id = %s",
                (body.requestId,),
            )
            existing = cur.fetchone()
        if existing:
            age_seconds = (datetime.now(timezone.utc) - existing["created_at"].replace(tzinfo=timezone.utc)).total_seconds()
            if age_seconds < 24 * 3600:
                if existing["status"] == "success" and existing.get("output") is not None:
                    output_data = existing.get("output") or {}
                    if isinstance(output_data, str):
                        try:
                            output_data = json.loads(output_data)
                        except (ValueError, TypeError):
                            output_data = {}
                    return {"ok": True, "response": {
                        "requestId": existing["request_id"],
                        "agentId": existing["agent_id"],
                        "promptVersion": 0,
                        "modelId": existing["model_id"],
                        "output": output_data,
                        "confidence": float(existing["confidence"]) if existing.get("confidence") else 0.0,
                        "sources": [],
                        "coverageGaps": [],
                        "riskFlags": [],
                        "latencyMs": existing["latency_ms"] or 0,
                        "generatedAt": existing["created_at"].isoformat(),
                    }}
                return {"ok": False, "failure": {
                    "requestId": existing["request_id"],
                    "agentId": existing["agent_id"],
                    "status": existing["status"],
                    "reason": existing.get("error_message") or "cached_failure",
                    "latencyMs": existing["latency_ms"] or 0,
                }}

    # Load agent
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM agents WHERE id = %s", (body.agentId,))
        agent = cur.fetchone()

    if not agent or not agent.get("is_enabled"):
        latency_ms = int((time.monotonic() - started_at) * 1000)
        failure = {
            "requestId": request_id,
            "agentId": body.agentId,
            "status": "rejected" if agent else "error",
            "reason": "agent_disabled" if agent else "agent_not_found",
            "latencyMs": latency_ms,
        }
        _persist_invocation(conn, body, request_id, user, None, latency_ms,
                            status=failure["status"], error_message=failure["reason"])
        return {"ok": False, "failure": failure}

    # Run simulation
    sim = _simulate_agent(body.agentId, body.variables)
    latency_ms = int((time.monotonic() - started_at) * 1000)

    # Persist invocation
    version_id = agent.get("active_prompt_id")
    _persist_invocation(
        conn, body, request_id, user, version_id, latency_ms,
        status="success", output=sim["output"], confidence=sim["confidence"],
    )

    write_audit(
        actor_id=str(user.get("id", "")),
        actor_email=user.get("email"),
        actor_role=user.get("role"),
        action="agent.invoke",
        target="agent_invocation",
        target_id=request_id,
        service="superadmin-service",
        ip_address=request.client.host if request.client else None,
        extra={"agentId": body.agentId, "promptName": body.promptName, "latencyMs": latency_ms},
    )

    return {"ok": True, "response": {
        "requestId": request_id,
        "agentId": body.agentId,
        "promptVersion": 1,
        "modelId": agent.get("model_id", "glimmora-mock-v1"),
        "output": sim["output"],
        "confidence": sim["confidence"],
        "sources": sim.get("sources", []),
        "coverageGaps": sim.get("coverageGaps", []),
        "riskFlags": sim.get("riskFlags", []),
        "latencyMs": latency_ms,
        "costCents": None,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }}


def _persist_invocation(
    conn,
    body: AgentInvokeRequest,
    request_id: str,
    user: dict,
    prompt_version_id: str | None,
    latency_ms: int,
    *,
    status: str = "success",
    output: Any = None,
    confidence: float | None = None,
    error_message: str | None = None,
) -> None:
    """Best-effort: never raises — invocation log must not block the response."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_invocations
                    (agent_id, prompt_version_id, model_id, tenant_id, actor_user_id,
                     request_id, input, output, confidence, latency_ms, status, error_message)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (request_id) DO NOTHING
                """,
                (
                    body.agentId,
                    prompt_version_id,
                    "glimmora-mock-v1",
                    user.get("tenant_id"),
                    str(user.get("id", "")),
                    request_id,
                    Json(body.variables),
                    Json(output) if output is not None else None,
                    confidence,
                    latency_ms,
                    status,
                    error_message,
                ),
            )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_invocations insert failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass


# ── GET /api/v1/matching/tasks/{taskId}/candidates ────────────────────────────

def _matching_score(profile: dict, task_skills: list[str]) -> float:
    """Simple keyword-overlap matching score in [0, 1]."""
    if not task_skills:
        return 0.5
    profile_skills: list[str] = []
    for field in ("primary_skills", "secondary_skills", "other_skills"):
        val = profile.get(field) or []
        if isinstance(val, list):
            profile_skills.extend(val)
    if not profile_skills:
        return 0.1
    task_lower = {s.lower() for s in task_skills}
    profile_lower = {s.lower() for s in profile_skills}
    matched = task_lower & profile_lower
    score = len(matched) / len(task_lower)
    return round(min(1.0, score + 0.1), 2)  # +0.1 baseline so even partial matches show well


@router.get("/api/v1/matching/tasks/{taskId}/candidates")
async def get_task_candidates(
    taskId: str,
    admin: Annotated[dict, Depends(get_current_admin)],
    limit: int = 20,
    offset: int = 0,
):
    """
    Return top contributor candidates for a task.
    Reads contributor_profiles joined to login_accounts.
    The `taskId` can be a task row id or a skill-tag string; we derive
    the skill list from admin_records (task rows) when available,
    otherwise use taskId tokens as skill hints.
    """
    conn = _conn()

    # Resolve task skills: look up the task in admin_records first
    task_skills: list[str] = []
    task_meta: dict = {}
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM admin_records WHERE id = %s AND kind IN ('task','sow_task') "
            "AND deleted_at IS NULL",
            (taskId,),
        )
        task_row = cur.fetchone()

    if task_row:
        raw_data = task_row.get("data") or {}
        if isinstance(raw_data, str):
            try:
                raw_data = json.loads(raw_data)
            except (ValueError, TypeError):
                raw_data = {}
        task_meta = {
            "id": task_row["id"],
            "name": task_row.get("name"),
            "status": task_row.get("status"),
        }
        # Skills may be stored as data.skillTags or data.skills
        for field in ("skillTags", "skills", "skill_tags"):
            val = raw_data.get(field)
            if isinstance(val, list):
                task_skills.extend(val)
                break
    else:
        # Treat hyphen/underscore-separated taskId tokens as skill hints
        task_skills = [t.replace("-", " ").replace("_", " ") for t in taskId.split("-")]
        task_meta = {"id": taskId, "name": None, "status": None}

    # Fetch contributor profiles with account info
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT cp.*,
                   la.email, la.first_name, la.last_name, la.name AS account_name,
                   la.is_active, la.role,
                   la.last_login_at
              FROM contributor_profiles cp
              JOIN login_accounts la ON la.id = cp.account_id
             WHERE la.is_active = TRUE
               AND (la.approval_status IS NULL OR la.approval_status = 'approved')
             ORDER BY cp.created_at DESC
            """,
        )
        profiles = cur.fetchall()

    # Score and sort
    candidates = []
    for p in profiles:
        score = _matching_score(dict(p), task_skills)
        candidate = {
            "contributorId": str(p["account_id"]),
            "email": p.get("email"),
            "name": (p.get("account_name")
                     or f"{p.get('first_name','') or ''} {p.get('last_name','') or ''}".strip()
                     or p.get("email")),
            "role": p.get("role"),
            "primarySkills": p.get("primary_skills") or [],
            "secondarySkills": p.get("secondary_skills") or [],
            "otherSkills": p.get("other_skills") or [],
            "availability": p.get("availability"),
            "weeklyHours": float(p["weekly_hours"]) if p.get("weekly_hours") is not None else None,
            "careerStage": p.get("career_stage"),
            "yearsExperience": p.get("years_experience"),
            "country": p.get("country"),
            "city": p.get("city"),
            "timezone": p.get("timezone"),
            "segment": p.get("segment"),
            "lastLoginAt": p.get("last_login_at").isoformat() if p.get("last_login_at") else None,
            "matchingScore": score,
        }
        candidates.append(candidate)

    candidates.sort(key=lambda c: c["matchingScore"], reverse=True)
    total = len(candidates)
    page_candidates = candidates[offset: offset + limit]

    return {
        "taskId": taskId,
        "task": task_meta,
        "taskSkills": task_skills,
        "candidates": page_candidates,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
