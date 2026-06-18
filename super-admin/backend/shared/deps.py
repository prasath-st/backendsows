"""
Shared FastAPI auth dependencies. Decode the Bearer access token and expose
the current user; role guards for admin/superadmin/enterprise/etc.
"""

from __future__ import annotations

import secrets
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from shared.config import settings
from shared.security import decode_token

# Declaring the scheme this way registers an HTTP "bearer" securityScheme in the
# OpenAPI schema, so Swagger /docs shows the "Authorize" button and a lock icon
# on protected routes. auto_error=False keeps our own "Missing bearer token"
# message (instead of FastAPI's default 403) and lets public routes stay open.
_bearer_scheme = HTTPBearer(auto_error=False, description="Paste the access_token from /api/v1/auth/login")

# Paths that must work WITHOUT the passcode: liveness/metrics, the API docs,
# and the auth bootstrap routes (you need these to obtain a JWT in the first
# place). Every other route requires the shared passcode when one is configured.
_PASSCODE_EXEMPT_PREFIXES = (
    "/health", "/healthz", "/metrics",
    "/docs", "/redoc", "/openapi.json",
    "/api/v1/auth",
)


async def verify_passcode(
    request: Request,
    api_passcode: Annotated[
        str | None,
        Header(alias="API-Passcode", description="Use the shared API-Passcode"),
    ] = None,
) -> None:
    """Global gate: require the shared passcode via the `API-Passcode` header on
    every non-exempt route (all methods). Shows as a fillable field in Swagger.
    Disabled when GlimmoraTeam_Passcode is unset. Constant-time compare.
    """
    expected = settings.api_passcode
    if not expected:
        return
    if any(request.url.path.startswith(p) for p in _PASSCODE_EXEMPT_PREFIXES):
        return
    if not isinstance(api_passcode, str) or not secrets.compare_digest(api_passcode, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing API passcode")


def _unauthorised(msg: str = "Not authenticated"):
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=msg)


def get_bearer_token(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> str:
    if creds is None or (creds.scheme or "").lower() != "bearer" or not creds.credentials:
        raise _unauthorised("Missing bearer token")
    return creds.credentials


def get_current_user(token: Annotated[str, Depends(get_bearer_token)]) -> dict:
    """Decode an *access* token → user claims {sub, email, role, ...}."""
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise _unauthorised("Token expired")
    except jwt.PyJWTError:
        raise _unauthorised("Invalid token")
    if payload.get("purpose") not in (None, "access"):
        raise _unauthorised("Wrong token type")
    return {
        "id": payload.get("sub"),
        "email": payload.get("email"),
        "role": payload.get("role"),
        "tenant_id": payload.get("tenant_id"),
        "claims": payload,
    }


def require_roles(*roles: str):
    """Dependency factory enforcing the user's role is in `roles`."""
    allowed = {r.lower() for r in roles}

    def _guard(user: Annotated[dict, Depends(get_current_user)]) -> dict:
        if (user.get("role") or "").lower() not in allowed:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return _guard


def get_current_admin(user: Annotated[dict, Depends(get_current_user)]) -> dict:
    if (user.get("role") or "").lower() not in {"admin", "superadmin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def get_current_superadmin(user: Annotated[dict, Depends(get_current_user)]) -> dict:
    if (user.get("role") or "").lower() not in {"superadmin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Super-admin access required")
    return user
