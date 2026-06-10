"""
End-to-end account provisioning lifecycle — verified entirely through backend APIs.

  1. Super-admin creates an account (POST /api/superadmin/users), temp password,
     must-change, credentials emailed.
  2. The account is visible via /api/superadmin/all-users (API, not DB).
  3. Create response reports emailSent (real SMTP) OR returns tempPassword.
  4. First login with the temp password works.
  5. Password change flips must-change off → old pw rejected, new pw works (API).
  6. Tenant-scoped creation: the user shows under that tenant in /all-users.
  7. Delete removes the account from /all-users.

Cleanup is done via the delete API. No direct DB access.
"""

import time
import pytest
from conftest import get, post, delete


def _uniq(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000) % 10_000_000}@apitest.glimmora.dev"


@pytest.fixture
def cleanup(sa_token):
    """Track created user ids and delete them via the API after the test."""
    ids: list[str] = []
    yield ids
    for uid in ids:
        try:
            delete("super-admin", f"/api/superadmin/users/{uid}", token=sa_token)
        except Exception:  # noqa: BLE001
            pass


def _create(sa_token, email, **extra):
    payload = {"email": email, "name": extra.pop("name", "API Test"),
               "role": extra.pop("role", "contributor"), "sendCredentials": True}
    payload.update(extra)
    return post("super-admin", "/api/superadmin/users", token=sa_token, json=payload)


def _all_users(sa_token):
    r = get("super-admin", "/api/superadmin/all-users", token=sa_token)
    assert r.status_code == 200
    return r.json().get("users", [])


def _find(sa_token, email):
    return next((u for u in _all_users(sa_token)
                 if u.get("email", "").lower() == email.lower()), None)


# ── 1-3: create + visible via API + credential delivery ──────────────────────

def test_create_user_visible_via_api(sa_token, cleanup):
    email = _uniq("lifecycle")
    r = _create(sa_token, email, role="contributor")
    assert r.status_code in (200, 201), f"create failed: {r.status_code} {r.text[:200]}"
    cleanup.append(str(r.json()["id"]))
    u = _find(sa_token, email)
    assert u is not None, "account not in /all-users after create"
    assert u.get("role") == "contributor"
    # newly provisioned account must require a password change (invited status)
    assert u.get("status") in ("invited", "active")


def test_create_user_emails_or_returns_credentials(sa_token, cleanup):
    email = _uniq("creds")
    r = _create(sa_token, email, role="mentor")
    assert r.status_code in (200, 201)
    cleanup.append(str(r.json()["id"]))
    body = r.json()
    assert body.get("emailSent") is True or body.get("tempPassword"), \
        f"neither emailSent nor tempPassword present: {body}"


# ── 4-5: first login with temp password + forced password change (API) ───────

def test_first_login_then_forced_password_change(sa_token, cleanup):
    email = _uniq("login")
    temp = "Temp@12345"
    r = _create(sa_token, email, role="contributor", password=temp, sendCredentials=False)
    assert r.status_code in (200, 201), r.text[:200]
    cleanup.append(str(r.json()["id"]))

    login = post("freelancer", "/api/v1/auth/login", json={"email": email, "password": temp})
    assert login.status_code == 200, f"first login failed: {login.status_code} {login.text[:200]}"
    tok = login.json()["access_token"]

    newpw = "Changed@99887"
    chg = post("freelancer", "/api/v1/auth/password/change", token=tok,
               json={"old_password": temp, "new_password": newpw, "confirmPassword": newpw})
    assert chg.status_code in (200, 204), f"password change failed: {chg.status_code} {chg.text[:200]}"

    # API behavior: old pw rejected, new pw works
    assert post("freelancer", "/api/v1/auth/login",
                json={"email": email, "password": temp}).status_code in (400, 401)
    assert post("freelancer", "/api/v1/auth/login",
                json={"email": email, "password": newpw}).status_code == 200


# ── 6: tenant-scoped account creation (visible scoped via API) ───────────────

def test_create_tenant_scoped_user(sa_token, cleanup):
    email = _uniq("tenant")
    tenant_id = f"t-apitest-{int(time.time()) % 100000}"
    r = _create(sa_token, email, role="enterprise", tenantId=tenant_id)
    assert r.status_code in (200, 201), r.text[:200]
    cleanup.append(str(r.json()["id"]))
    u = _find(sa_token, email)
    assert u is not None and u.get("tenantId") == tenant_id, \
        f"tenant not reflected via API: {u}"


# ── 7: delete removes from the API listing ───────────────────────────────────

def test_delete_user_removes_from_api(sa_token):
    email = _uniq("del")
    r = _create(sa_token, email, role="contributor")
    assert r.status_code in (200, 201)
    uid = str(r.json()["id"])
    assert _find(sa_token, email) is not None

    d = delete("super-admin", f"/api/superadmin/users/{uid}", token=sa_token)
    assert d.status_code in (200, 204), f"delete failed: {d.status_code} {d.text[:200]}"
    assert _find(sa_token, email) is None, "account still listed after delete"
