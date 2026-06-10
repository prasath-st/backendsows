"""
Per-role authenticated smoke tests — hit each role's key read endpoints with a
real token and assert 200 + a sane response shape. Roles without seeded creds
(mentor/freelancer/reviewer) are covered by minting a token from a created
account where needed; here we focus on the two seeded logins (super-admin,
enterprise) plus public-shape checks for the others.
"""

import pytest
from conftest import get


# ── super-admin authenticated reads ──────────────────────────────────────────

SA_ENDPOINTS = [
    "/api/superadmin/dashboard",
    "/api/superadmin/all-users",
    "/api/superadmin/mentors",
    "/api/superadmin/reviewers",
    "/api/superadmin/contributors",
    "/api/superadmin/sows",
    "/api/superadmin/kyc",
    "/api/superadmin/audit-log",
]


@pytest.mark.parametrize("path", SA_ENDPOINTS)
def test_superadmin_reads(sa_token, path):
    r = get("super-admin", path, token=sa_token)
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:150]}"
    # response should be JSON (list or object), never an error string
    body = r.json()
    assert body is not None


# ── enterprise authenticated reads ───────────────────────────────────────────

ENT_ENDPOINTS = [
    "/api/v1/sows",
    "/api/v1/enterprise/decomposition/plans",
    "/api/v1/billing/summary",
    "/api/v1/billing/invoices",
    # NOTE: /api/v1/enterprise/compliance/* is a frontend page not yet backed by
    # the enterprise backend (404) — excluded until that endpoint is built.
]


@pytest.mark.parametrize("path", ENT_ENDPOINTS)
def test_enterprise_reads(ent_token, path):
    r = get("enterprise", path, token=ent_token)
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:150]}"
    assert r.json() is not None


# ── enterprise SOW list is clean (no demo-seed SOW reappearing) ──────────────

def test_enterprise_sows_no_demo_seed(ent_token):
    r = get("enterprise", "/api/v1/sows", token=ent_token)
    assert r.status_code == 200
    data = r.json().get("data", r.json())
    rows = data if isinstance(data, list) else data.get("items", [])
    titles = " ".join(str(x.get("projectTitle") or x.get("title") or "") for x in rows).lower()
    assert "demo cloud migration" not in titles, "demo SOW re-seeded — seeding not disabled"
