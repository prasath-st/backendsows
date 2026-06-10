"""
OAuth router — /api/v1/auth/oauth/** (Google + Microsoft, contributors only).

Authorize endpoints 302-redirect to the provider. Callbacks exchange the code,
find-or-create a contributor login_accounts row, and return a token pair built
with shared.security. Diagnostic endpoints return config hints for setup.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from shared.audit import write_audit
from shared.config import settings
from shared.kafka_bus import publish_event
from shared.security import (
    access_token_seconds,
    create_access_token,
    create_refresh_token,
)

from contributor_app import db, oauth

router = APIRouter(prefix="/api/v1/auth", tags=["contributor-oauth"])


def _token_pair(account: dict) -> dict:
    claims = {
        "sub": str(account["id"]),
        "email": account["email"],
        "role": account.get("role") or "contributor",
        "tenant_id": account.get("tenant_id"),
    }
    return {
        "access_token": create_access_token(claims),
        "refresh_token": create_refresh_token(claims),
        "token_type": "bearer",
        "expires_in": access_token_seconds(),
        "user": {
            "id": str(account["id"]),
            "firstName": account.get("first_name") or "",
            "lastName": account.get("last_name") or "",
            "email": account["email"],
            "role": account.get("role") or "contributor",
        },
    }


# ── authorize (302 redirect to provider) ─────────────────────────────────────

@router.get("/oauth/google/authorize")
async def google_authorize(state: str | None = None):
    return RedirectResponse(url=oauth.build_google_authorize_url(state), status_code=302)


@router.get("/oauth/microsoft/authorize")
async def microsoft_authorize(state: str | None = None):
    return RedirectResponse(url=oauth.build_microsoft_authorize_url(state), status_code=302)


# ── authorize-as-json (used by frontend that wants the URL, not a redirect) ───

@router.get("/contributor/oauth/google/authorize")
async def google_authorize_json(state: str | None = None):
    return {"url": oauth.build_google_authorize_url(state)}


@router.get("/contributor/oauth/microsoft/authorize")
async def microsoft_authorize_json(state: str | None = None):
    return {"url": oauth.build_microsoft_authorize_url(state)}


# ── callbacks (exchange code → token pair) ────────────────────────────────────

async def _do_callback(provider: str, code: str, request: Request) -> dict:
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")
    try:
        if provider == "google":
            info = await oauth.exchange_google(code)
        else:
            info = await oauth.exchange_microsoft(code)
    except oauth.OAuthNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=400, detail=f"OAuth exchange failed: {exc}")

    email = (info.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Provider did not return an email address")

    account = db.create_oauth_account(
        email=email,
        first_name=info.get("first_name", ""),
        last_name=info.get("last_name", ""),
        provider=provider,
    )
    db.execute("UPDATE login_accounts SET last_login_at = now() WHERE id = %s", (account["id"],))

    publish_event("user.oauth_login", {"userId": str(account["id"]), "email": email,
                                        "provider": provider})
    write_audit(actor_id=str(account["id"]), actor_email=email, actor_role="contributor",
                action=f"oauth_login_{provider}", service="contributor-service",
                ip_address=request.client.host if request.client else None)
    return _token_pair(account)


@router.get("/oauth/google/callback")
async def google_callback(request: Request, code: str = "", state: str | None = None):
    return await _do_callback("google", code, request)


@router.get("/oauth/microsoft/callback")
async def microsoft_callback(request: Request, code: str = "", state: str | None = None):
    return await _do_callback("microsoft", code, request)


# ── diagnostics ────────────────────────────────────────────────────────────

@router.get("/oauth/google/diagnostic")
async def google_diagnostic(request: Request):
    return {
        "google_oauth_configured": oauth.google_configured(),
        "redirect_uri_register_this_exactly_in_google": oauth.google_redirect_uri(),
        "request_base_url": str(request.base_url).rstrip("/"),
        "oauth_public_base_url": settings.oauth_public_base_url,
        "client_id_present": bool(settings.google_client_id.strip()),
        "client_secret_present": bool(settings.google_client_secret.strip()),
        "tips": [
            "Register the redirect URI above as an Authorized redirect URI in the "
            "Google Cloud Console OAuth client.",
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in the service environment.",
            "oauth_public_base_url must be the public origin the browser is redirected to.",
        ],
    }


@router.get("/oauth/microsoft/diagnostic")
async def microsoft_diagnostic(request: Request):
    return {
        "microsoft_oauth_configured": oauth.microsoft_configured(),
        "redirect_uri_register_this_exactly_in_azure": oauth.microsoft_redirect_uri(),
        "microsoft_tenant_id": settings.microsoft_tenant_id or "common",
        "request_base_url": str(request.base_url).rstrip("/"),
        "oauth_public_base_url": settings.oauth_public_base_url,
        "pkce": False,
        "tips": [
            "Register the redirect URI above under Authentication → Web in the Azure "
            "app registration.",
            "Set MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET and MICROSOFT_TENANT_ID.",
            "Use tenant 'common' for multi-tenant + personal accounts, or your tenant GUID.",
        ],
    }
