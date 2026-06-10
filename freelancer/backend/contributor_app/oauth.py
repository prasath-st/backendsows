"""
OAuth helpers for contributor sign-in (Google + Microsoft).

The contributor-service owns OAuth for contributors only. Authorize URLs are
always buildable from settings (even with empty client id/secret — the keys are
plugged in later). The token-exchange + userinfo calls require real keys; if
they're missing we raise a clear error the router turns into a 400.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

from shared.config import settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

MS_SCOPE = "openid profile email User.Read"
GOOGLE_SCOPE = "openid email profile"


# ── redirect URIs ──────────────────────────────────────────────────────────

def google_redirect_uri() -> str:
    return f"{settings.oauth_public_base_url.rstrip('/')}/api/v1/auth/oauth/google/callback"


def microsoft_redirect_uri() -> str:
    return f"{settings.oauth_public_base_url.rstrip('/')}/api/v1/auth/oauth/microsoft/callback"


def _ms_authority() -> str:
    tenant = settings.microsoft_tenant_id or "common"
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


# ── authorize URL builders (always buildable) ────────────────────────────────

def build_google_authorize_url(state: str | None = None) -> str:
    params = {
        "client_id": settings.google_client_id or "GOOGLE_CLIENT_ID_NOT_SET",
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": GOOGLE_SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
    }
    if state:
        params["state"] = state
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def build_microsoft_authorize_url(state: str | None = None) -> str:
    params = {
        "client_id": settings.microsoft_client_id or "MICROSOFT_CLIENT_ID_NOT_SET",
        "redirect_uri": microsoft_redirect_uri(),
        "response_type": "code",
        "scope": MS_SCOPE,
        "response_mode": "query",
    }
    if state:
        params["state"] = state
    return f"{_ms_authority()}/authorize?{urlencode(params)}"


# ── configuration checks ──────────────────────────────────────────────────────

def google_configured() -> bool:
    return bool(settings.google_client_id.strip() and settings.google_client_secret.strip())


def microsoft_configured() -> bool:
    return bool(settings.microsoft_client_id.strip() and settings.microsoft_client_secret.strip())


class OAuthNotConfigured(Exception):
    """Raised when a token exchange is attempted without provider keys."""


# ── token exchange + userinfo ─────────────────────────────────────────────────

async def exchange_google(code: str) -> dict:
    """Exchange a Google auth code -> {email, first_name, last_name}."""
    if not google_configured():
        raise OAuthNotConfigured(
            "Google OAuth keys are not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET, and register the redirect URI "
            f"{google_redirect_uri()} in the Google Cloud console."
        )
    async with httpx.AsyncClient(timeout=30) as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": google_redirect_uri(),
            "grant_type": "authorization_code",
        })
        token_resp.raise_for_status()
        access = token_resp.json().get("access_token")
        info_resp = await client.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access}"}
        )
        info_resp.raise_for_status()
        info = info_resp.json()
    return {
        "email": info.get("email", ""),
        "first_name": info.get("given_name", "") or info.get("name", "").split(" ")[0],
        "last_name": info.get("family_name", ""),
    }


async def exchange_microsoft(code: str) -> dict:
    """Exchange a Microsoft auth code -> {email, first_name, last_name}."""
    if not microsoft_configured():
        raise OAuthNotConfigured(
            "Microsoft OAuth keys are not configured. Set MICROSOFT_CLIENT_ID and "
            "MICROSOFT_CLIENT_SECRET, set MICROSOFT_TENANT_ID, and register the "
            f"redirect URI {microsoft_redirect_uri()} in the Azure app registration."
        )
    async with httpx.AsyncClient(timeout=30) as client:
        token_resp = await client.post(f"{_ms_authority()}/token", data={
            "code": code,
            "client_id": settings.microsoft_client_id,
            "client_secret": settings.microsoft_client_secret,
            "redirect_uri": microsoft_redirect_uri(),
            "grant_type": "authorization_code",
            "scope": MS_SCOPE,
        })
        token_resp.raise_for_status()
        access = token_resp.json().get("access_token")
        info_resp = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access}"},
        )
        info_resp.raise_for_status()
        info = info_resp.json()
    email = info.get("mail") or info.get("userPrincipalName", "")
    return {
        "email": email,
        "first_name": info.get("givenName", ""),
        "last_name": info.get("surname", ""),
    }
