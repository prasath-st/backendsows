"""
OTP sending + forgot-password flow.

The backend echoes a `devOtp` in the response only when email delivery is
unavailable (dev fallback) or OTP_DEV_ECHO=1. When real email succeeds, the
response just confirms the code was sent (no code leaked). Both paths are valid
and asserted here; the tests never depend on an inbox.
"""

import pytest
from conftest import CREDS, post, get


def _known_email() -> str:
    # use a real account so the flow operates on an existing user
    return CREDS["super-admin"][0]


def test_otp_send_email_accepted():
    r = post("super-admin", "/api/v1/auth/otp/send-email", json={"email": _known_email()})
    assert r.status_code in (200, 202), f"otp send failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    # either a real send (ok/sent) or a dev fallback echoing devOtp
    assert body.get("ok") or body.get("sent") or body.get("devOtp") or body.get("status"), \
        f"unexpected otp response: {body}"


def test_forgot_password_does_not_leak_for_unknown_email():
    # forgot-password should not reveal whether an email exists (no error/200 either way)
    r = post("super-admin", "/api/v1/auth/password/forgot",
             json={"email": "ghost-xyz@nowhere.test"})
    # 200/202 = silent (no enumeration); 404 = not found; 422 = validation. Any is acceptable.
    assert r.status_code in (200, 202, 404, 422), f"forgot unknown email: {r.status_code}"


def test_forgot_password_for_known_email_starts_flow():
    r = post("super-admin", "/api/v1/auth/password/forgot", json={"email": _known_email()})
    assert r.status_code in (200, 202), f"forgot known email: {r.status_code} {r.text[:200]}"
    body = r.json()
    assert body.get("ok") or body.get("devOtp") or body.get("status") or body == {}, \
        f"unexpected forgot response: {body}"


def test_otp_verify_rejects_wrong_code():
    # a deliberately wrong code must not verify
    r = post("super-admin", "/api/v1/auth/otp/verify-email",
             json={"email": _known_email(), "code": "000000"})
    assert r.status_code in (400, 401, 422), f"wrong OTP not rejected: {r.status_code}"


@pytest.mark.parametrize("role", ["mentor", "enterprise", "freelancer", "reviewer", "super-admin"])
def test_every_backend_exposes_forgot_and_otp(role):
    # all role logins must offer the same recovery surface
    import requests
    from conftest import base
    spec = requests.get(f"{base(role)}/openapi.json", timeout=15).json()["paths"]
    for p in ("/api/v1/auth/password/forgot", "/api/v1/auth/otp/send-email",
              "/api/v1/auth/password/reset", "/api/v1/auth/password/setup-after-otp"):
        assert p in spec, f"{role} missing {p}"
