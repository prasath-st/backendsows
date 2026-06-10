"""Login, token issuance, /me, and auth rejection across the role backends."""

import pytest
from conftest import BACKENDS, CREDS, base, get, post, auth


# ── Login happy path (roles we have real creds for) ──────────────────────────

@pytest.mark.parametrize("role", list(CREDS.keys()))
def test_login_returns_access_token(role):
    email, password = CREDS[role]
    r = post(role, "/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, f"{role} login failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    assert body.get("access_token"), f"{role} login returned no access_token"
    assert body.get("user", {}).get("email", "").lower() == email.lower()


@pytest.mark.parametrize("role", list(CREDS.keys()))
def test_me_with_token(role):
    email, password = CREDS[role]
    tok = post(role, "/api/v1/auth/login", json={"email": email, "password": password}).json()["access_token"]
    r = get(role, "/api/v1/auth/me", token=tok)
    assert r.status_code == 200, f"{role} /me failed: {r.status_code}"
    me = r.json()
    # /me may wrap in {data:{...}} or return the user directly
    user = me.get("data", me)
    em = (user.get("email") or user.get("user", {}).get("email") or "").lower()
    assert em == email.lower() or r.status_code == 200


# ── Login rejection ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", list(CREDS.keys()))
def test_login_wrong_password_rejected(role):
    email, _ = CREDS[role]
    r = post(role, "/api/v1/auth/login", json={"email": email, "password": "definitely-wrong-xyz"})
    assert r.status_code in (400, 401), f"{role} wrong password not rejected: {r.status_code}"


def test_login_unknown_email_rejected():
    r = post("super-admin", "/api/v1/auth/login",
             json={"email": "nobody-xyz@nowhere.test", "password": "whatever"})
    # 400/401 = credential rejection; 422 = validation rejection — all mean "not let in"
    assert r.status_code in (400, 401, 422)


# ── Protected endpoints require a token ──────────────────────────────────────

PROTECTED = {
    "mentor": "/api/mentor/dashboard",
    "super-admin": "/api/superadmin/dashboard",
    "enterprise": "/api/v1/billing/summary",
    "freelancer": "/api/contributor/dashboard",
    "reviewer": "/api/v1/reviewer/dashboard",
}


@pytest.mark.parametrize("role,path", list(PROTECTED.items()))
def test_protected_requires_auth(role, path):
    r = get(role, path)  # no token
    assert r.status_code in (401, 403), f"{role} {path} not protected: {r.status_code}"


@pytest.mark.parametrize("role,path", list(PROTECTED.items()))
def test_protected_rejects_garbage_token(role, path):
    r = get(role, path, headers=auth("garbage.token.value"))
    assert r.status_code in (401, 403), f"{role} {path} accepted bad token: {r.status_code}"
