"""
Shared FastAPI auth dependencies. Decode the Bearer access token and expose
the current user; role guards for admin/superadmin/enterprise/etc.
"""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, status

from shared.security import decode_token


def _unauthorised(msg: str = "Not authenticated"):
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=msg)


def get_bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorised("Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


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
