"""
Password hashing + JWT issuance/validation, shared across services so tokens
issued by one service decode in any other (same API_SECRET_KEY).

Token kinds (via `purpose` claim):
  - access   : normal API access token  (sub, email, role)
  - refresh  : refresh token
  - mfa_pending : short-lived token issued mid-login when MFA is required
  - reset    : password-reset token
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from shared.config import settings


# ── Passwords ────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def generate_temp_password(length: int = 12) -> str:
    """Readable temporary password (mixed case + digits + one symbol)."""
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(length - 1))
    return body + secrets.choice("!@#$%&*")


# ── JWT ──────────────────────────────────────────────────────────────────────

def _encode(payload: dict[str, Any], minutes: int, purpose: str) -> str:
    now = datetime.now(timezone.utc)
    body = {
        **payload,
        "purpose": purpose,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    return jwt.encode(body, settings.api_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(payload: dict[str, Any]) -> str:
    return _encode(payload, settings.access_token_expire_minutes, "access")


def create_refresh_token(payload: dict[str, Any]) -> str:
    return _encode(payload, settings.refresh_token_expire_minutes, "refresh")


def create_mfa_pending_token(payload: dict[str, Any], minutes: int = 10) -> str:
    return _encode(payload, minutes, "mfa_pending")


def create_reset_token(payload: dict[str, Any], minutes: int = 30) -> str:
    return _encode(payload, minutes, "reset")


def decode_token(token: str, expected_purpose: str | None = None) -> dict[str, Any]:
    payload = jwt.decode(token, settings.api_secret_key, algorithms=[settings.jwt_algorithm])
    if expected_purpose is not None and payload.get("purpose") != expected_purpose:
        raise jwt.InvalidTokenError("token purpose mismatch")
    return payload


def access_token_seconds() -> int:
    return settings.access_token_expire_minutes * 60
