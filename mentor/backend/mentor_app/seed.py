"""
Demo-data seeding for a mentor. Called lazily on the mentor's first access so
the portal pages aren't empty. Idempotent per mentor: a sentinel flag in the
mentor_profiles.settings JSONB prevents reseeding.
"""

from __future__ import annotations

import json
import logging

from psycopg2.extras import Json, RealDictCursor

from shared.db import ensure_pg_clean, get_pg_connection

logger = logging.getLogger(__name__)


def _already_seeded(cur, mentor_id: str) -> bool:
    cur.execute(
        "SELECT settings FROM mentor_profiles WHERE mentor_id = %s",
        (mentor_id,),
    )
    row = cur.fetchone()
    if not row:
        return False
    settings = row.get("settings") or {}
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except (ValueError, TypeError):
            settings = {}
    return bool(settings.get("_demo_seeded"))


def ensure_profile(cur, user: dict) -> None:
    """Create a mentor_profiles row if missing."""
    mentor_id = str(user["id"])
    cur.execute("SELECT 1 FROM mentor_profiles WHERE mentor_id = %s", (mentor_id,))
    if cur.fetchone():
        return
    name = (user.get("email") or "").split("@")[0].replace(".", " ").title() or "Mentor"
    cur.execute(
        """
        INSERT INTO mentor_profiles
            (mentor_id, tenant_id, display_name, headline, bio, expertise,
             languages, timezone, links, settings)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (mentor_id) DO NOTHING
        """,
        (
            mentor_id,
            user.get("tenant_id"),
            name,
            "Senior Mentor & Reviewer",
            "Experienced mentor guiding contributors through quality reviews.",
            Json(["Curriculum Design", "Content Review", "Assessment"]),
            Json(["English"]),
            "UTC",
            Json({}),
            Json({}),
        ),
    )


def seed_mentor_demo(user: dict) -> None:
    """Seed demo reviews/mentorships/escalations/notes for `user` once."""
    ensure_pg_clean()
    conn = get_pg_connection()
    mentor_id = str(user["id"])
    mentor_email = user.get("email")
    tenant_id = user.get("tenant_id")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        ensure_profile(cur, user)
        if _already_seeded(cur, mentor_id):
            conn.commit()
            return

        # ── Mentorships ──────────────────────────────────────────────────────
        mentees = [
            ("Aisha Khan", "aisha.khan@example.com", "contributor", "Curriculum", "active", 72),
            ("Diego Santos", "diego.santos@example.com", "contributor", "Assessment", "active", 40),
            ("Mei Lin", "mei.lin@example.com", "student", "Content Writing", "paused", 88),
        ]
        mentee_ids: list[int] = []
        for name, email, role, track, status, progress in mentees:
            cur.execute(
                """
                INSERT INTO mentor_mentorships
                    (mentor_id, tenant_id, mentee_name, mentee_email, role, track,
                     status, progress, goals, meta)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    mentor_id, tenant_id, name, email, role, track, status, progress,
                    Json([
                        {"title": "Complete onboarding module", "done": True},
                        {"title": "Submit first reviewed artifact", "done": progress > 50},
                    ]),
                    Json({"joined": "2026-04-12", "sessions": 3}),
                ),
            )
            mentee_ids.append(cur.fetchone()["id"])

        # ── Reviews (pending + completed) ────────────────────────────────────
        reviews = [
            ("Algebra II — Unit 3 Lesson Plan", "content", "Aisha Khan", "high", "pending",
             None, None, None, mentee_ids[0]),
            ("Physics SOW — Term 2", "sow", "Diego Santos", "normal", "in_review",
             None, None, None, mentee_ids[1]),
            ("Biology Quiz Bank", "assessment", "Mei Lin", "low", "accepted",
             "accept", 4.6, "Strong coverage; minor wording fixes applied.", mentee_ids[2]),
            ("History Essay Rubric", "content", "Aisha Khan", "normal", "rework",
             "rework", 3.1, "Needs clearer band descriptors before approval.", mentee_ids[0]),
        ]
        first_review_id = None
        for (title, stype, cname, prio, status, decision, score, comments, mid) in reviews:
            cur.execute(
                """
                INSERT INTO mentor_reviews
                    (mentor_id, mentor_email, tenant_id, title, submission_type,
                     contributor_name, priority, status, decision, score, comments,
                     payload, rubric, mentee_id,
                     decided_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        CASE WHEN %s IS NOT NULL THEN now() ELSE NULL END)
                RETURNING id
                """,
                (
                    mentor_id, mentor_email, tenant_id, title, stype, cname, prio, status,
                    decision, score, comments,
                    Json({"summary": f"Submission: {title}",
                          "sections": [{"name": "Overview", "ok": True},
                                       {"name": "Activities", "ok": status == "accepted"}]}),
                    Json({"clarity": score or 0, "accuracy": score or 0,
                          "alignment": score or 0}),
                    mid,
                    decision,
                ),
            )
            if first_review_id is None:
                first_review_id = cur.fetchone()["id"]
            else:
                cur.fetchone()

        # ── Escalation ───────────────────────────────────────────────────────
        cur.execute(
            """
            INSERT INTO mentor_escalations
                (mentor_id, mentor_email, tenant_id, review_id, mentee_id, subject,
                 category, priority, status, description, timeline, meta)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                mentor_id, mentor_email, tenant_id, first_review_id, mentee_ids[1],
                "Repeated quality issues in SOW submissions", "quality", "high", "open",
                "Contributor's last 3 submissions missed alignment criteria; "
                "requesting senior review.",
                Json([{"at": "2026-05-28T10:00:00Z", "status": "open",
                       "by": mentor_email, "note": "Escalation created"}]),
                Json({"submissions_affected": 3}),
            ),
        )

        # ── Notes ────────────────────────────────────────────────────────────
        cur.execute(
            """
            INSERT INTO mentor_notes (mentor_id, tenant_id, mentee_id, kind, body, attachments)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (
                mentor_id, tenant_id, mentee_ids[0], "mentorship",
                "Great progress on lesson structuring. Focus next on assessment alignment.",
                Json([]),
            ),
        )

        # mark seeded
        cur.execute(
            """
            UPDATE mentor_profiles
               SET settings = jsonb_set(COALESCE(settings, '{}'::jsonb),
                                        '{_demo_seeded}', 'true'::jsonb)
             WHERE mentor_id = %s
            """,
            (mentor_id,),
        )

    conn.commit()
    logger.info("Seeded mentor demo data for %s", mentor_id)
