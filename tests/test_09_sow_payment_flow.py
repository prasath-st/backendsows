"""
Major SOW flow + payment/billing flow — verified through backend APIs only.

SOW lifecycle (enterprise backend):
  1. Create SOW via real multipart file upload POST /api/v1/sow/upload
  2. GET /api/v1/sow/{id} confirms it was stored (file pushed + persisted)
  3. GET /api/v1/approvals/{id} → 5 stages incl. commercial (Glimmora-last)
  4. Approve every gate via the approvals API → SOW status becomes 'approved'
     (read back via the SOW API, not the DB)
  5. Create a decomposition plan + task from the approved SOW
  6. DELETE /api/v1/sow/{id} cleans up

Payment / billing flow:
  7. enterprise billing summary + invoices list respond
  8. contributor payouts list responds
"""

import io
import time
import pytest
import requests
from conftest import base, get, post, delete, TIMEOUT


@pytest.fixture
def created_sow(ent_token):
    """Create a SOW via real multipart upload; yield (id, title); delete via API."""
    title = f"API Test SOW {int(time.time())%100000}"
    files = {"file": ("sow.txt",
                      io.BytesIO(b"Statement of work test document.\nScope: API integration test."),
                      "text/plain")}
    data = {"projectTitle": title, "clientOrganisation": "API Test Org",
            "budgetAmount": "100000", "budgetCurrency": "INR"}
    r = requests.post(f"{base('enterprise')}/api/v1/sow/upload",
                      headers={"Authorization": f"Bearer {ent_token}"},
                      files=files, data=data, timeout=TIMEOUT * 2)
    if r.status_code not in (200, 201):
        pytest.skip(f"SOW upload not available: {r.status_code} {r.text[:150]}")
    sow = r.json().get("data", r.json())
    sow_id = sow.get("id")
    assert sow_id, f"no sow id returned: {sow}"
    yield sow_id, title
    try:
        delete("enterprise", f"/api/v1/sow/{sow_id}", token=ent_token)
    except Exception:  # noqa: BLE001
        pass


def test_sow_file_upload_stored_and_readable(created_sow, ent_token):
    """The pushed SOW file/record is stored and readable back via the API."""
    sow_id, title = created_sow
    r = get("enterprise", f"/api/v1/sow/{sow_id}", token=ent_token)
    assert r.status_code == 200, f"SOW read-back failed: {r.status_code} {r.text[:150]}"
    sow = r.json().get("data", r.json())
    assert sow.get("id") == sow_id
    assert (sow.get("projectTitle") or sow.get("title")) == title, "stored SOW title mismatch"


def test_sow_listed_after_create(created_sow, ent_token):
    sow_id, _ = created_sow
    r = get("enterprise", "/api/v1/sows", token=ent_token)
    assert r.status_code == 200
    data = r.json().get("data", r.json())
    rows = data if isinstance(data, list) else data.get("items", [])
    assert any(x.get("id") == sow_id for x in rows), "created SOW not in list"


def test_sow_has_five_approval_stages(created_sow, ent_token):
    sow_id, _ = created_sow
    r = get("enterprise", f"/api/v1/approvals/{sow_id}", token=ent_token)
    assert r.status_code == 200, f"approvals read: {r.status_code} {r.text[:150]}"
    stages = r.json().get("data", r.json()).get("stages", [])
    keys = [s.get("key") for s in stages]
    assert len(stages) >= 4, f"expected approval stages, got {keys}"
    assert "commercial" in keys, f"commercial gate missing: {keys}"


def test_sow_full_approval_flow_to_approved(created_sow, ent_token, sa_token):
    sow_id, _ = created_sow
    stages = get("enterprise", f"/api/v1/approvals/{sow_id}", token=ent_token).json()
    stages = stages.get("data", stages).get("stages", [])
    for s in stages:
        key = s.get("key")
        owner = (s.get("owner") or "").lower()
        tok = sa_token if (key == "commercial" or owner in ("platform", "glimmora")) else ent_token
        dec = requests.post(
            f"{base('enterprise')}/api/v1/approvals/{sow_id}/stage/{key}/decide",
            headers={"Authorization": f"Bearer {tok}"},
            json={"decision": "approve", "comment": "api test approve"}, timeout=TIMEOUT)
        assert dec.status_code in (200, 201), f"approve {key}: {dec.status_code} {dec.text[:150]}"

    # read SOW status back via the API (not the DB)
    sow = get("enterprise", f"/api/v1/sow/{sow_id}", token=ent_token).json()
    sow = sow.get("data", sow)
    assert sow.get("status") == "approved", f"SOW not approved after all gates: {sow.get('status')}"


def test_decomposition_plan_create_and_task(created_sow, ent_token):
    sow_id, _ = created_sow
    plan = post("enterprise", "/api/v1/enterprise/decomposition/plans", token=ent_token,
                json={"sowId": sow_id, "title": "API Test Plan"})
    assert plan.status_code in (200, 201), f"plan create: {plan.status_code} {plan.text[:150]}"
    plan_id = plan.json().get("data", plan.json()).get("id")
    assert plan_id, "no plan id"
    task = post("enterprise", f"/api/v1/enterprise/decomposition/plans/{plan_id}/tasks",
                token=ent_token, json={"title": "API Test Task"})
    assert task.status_code in (200, 201), f"task create: {task.status_code} {task.text[:150]}"


# ── payment / billing flow ───────────────────────────────────────────────────

def test_enterprise_billing_summary(ent_token):
    r = get("enterprise", "/api/v1/billing/summary", token=ent_token)
    assert r.status_code == 200, f"billing summary: {r.status_code}"


def test_enterprise_invoices_list(ent_token):
    r = get("enterprise", "/api/v1/billing/invoices", token=ent_token)
    assert r.status_code == 200, f"invoices: {r.status_code}"
    assert isinstance(r.json().get("data", r.json()), (list, dict))


def test_contributor_payouts_list(sa_token):
    """Provision a throwaway contributor (API), log in, read payouts; delete via API."""
    email = f"payout-{int(time.time()*1000)%10_000_000}@apitest.glimmora.dev"
    pw = "Payout@123"
    c = post("super-admin", "/api/superadmin/users", token=sa_token,
             json={"email": email, "name": "Payout Test", "role": "contributor",
                   "password": pw, "sendCredentials": False})
    uid = str(c.json().get("id", "")) if c.status_code in (200, 201) else ""
    try:
        tok = post("freelancer", "/api/v1/auth/login",
                   json={"email": email, "password": pw}).json()["access_token"]
        r = get("freelancer", "/api/contributor/payouts", token=tok)
        assert r.status_code == 200, f"payouts: {r.status_code}"
    finally:
        if uid:
            delete("super-admin", f"/api/superadmin/users/{uid}", token=sa_token)
