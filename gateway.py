"""
Gateway for the role-based backends.

Listens on :9000 (local) / $PORT (cloud) and reverse-proxies each API path
prefix to the matching per-role backend:

    mentor 8101 · super-admin 8102 · enterprise 8103 · freelancer 8104 · reviewer 8105

Shared auth (/api/v1/auth/*) is served by every backend, so it is routed to the
super-admin backend by default (any would do). Cloud deploys can override each
upstream with SERVICE_URL_<ROLE> (e.g. SERVICE_URL_ENTERPRISE).

Run:  cd backends ; python -m uvicorn gateway:app --host 127.0.0.1 --port 9000
"""

from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

# role -> local port
ROLE_PORTS = {
    "mentor": 8101,
    "super-admin": 8102,
    "enterprise": 8103,
    "freelancer": 8104,
    "reviewer": 8105,
}


def _base_url(role: str) -> str:
    """Cloud: SERVICE_URL_<ROLE> hostname. Local: 127.0.0.1:<port>."""
    env = os.getenv(f"SERVICE_URL_{role.upper().replace('-', '_')}")
    if env:
        return env.rstrip("/")
    return f"http://127.0.0.1:{ROLE_PORTS[role]}"


# Path-prefix -> role. Longest prefix wins (sorted below), so more specific
# prefixes (/api/superadmin) beat generic ones (/api).
ROUTES: list[tuple[str, str]] = [
    # shared auth — served by all; send to super-admin
    ("/api/v1/auth", "super-admin"),
    # super-admin / platform
    ("/api/superadmin", "super-admin"),
    ("/api/admin", "super-admin"),
    ("/api/v1/admin", "super-admin"),
    ("/api/ai", "super-admin"),
    ("/api/audit", "super-admin"),
    ("/api/email", "super-admin"),
    ("/api/file-scan", "super-admin"),
    ("/api/breadcrumb", "super-admin"),
    ("/api/v1/matching", "super-admin"),
    # reviewer (its endpoints live under /api/v1/reviewer)
    ("/api/v1/reviewer", "reviewer"),
    # mentor
    ("/api/mentor", "mentor"),
    ("/api/v1/mentor", "mentor"),
    # user login-session management (lives in mentor's auth_app)
    ("/api/v1/sessions", "mentor"),
    ("/api/sessions", "mentor"),
    # contributor / freelancer
    ("/api/contributor", "freelancer"),
    ("/api/public/credentials", "freelancer"),
    ("/api/v1/submissions", "freelancer"),
    ("/api/v1/payouts", "freelancer"),
    ("/api/v1/notifications", "freelancer"),
    # enterprise
    ("/api/v1/razorpay", "enterprise"),
    ("/api/v1/sows", "enterprise"),
    ("/api/v1/sow", "enterprise"),
    ("/api/v1/approvals", "enterprise"),
    ("/api/v1/enterprise", "enterprise"),
    ("/api/v1/billing", "enterprise"),
    ("/api/v1/wizards", "enterprise"),
    ("/api/v1/portfolio", "enterprise"),
    ("/api/v1/projects", "enterprise"),
    ("/api/v1/review", "enterprise"),
    ("/api/v1/internal", "enterprise"),
    ("/api/enterprise", "enterprise"),
]
ROUTES.sort(key=lambda r: len(r[0]), reverse=True)

app = FastAPI(title="gtproject-backends-gateway")
_client = httpx.AsyncClient(timeout=120.0)


def _target(path: str) -> str | None:
    for prefix, role in ROUTES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix):
            return role
    return None


@app.get("/")
async def root():
    return {"ok": True, "service": "gtproject-backends-gateway", "roles": list(ROLE_PORTS)}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "gateway": "backends"}


@app.api_route("/{full_path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy(full_path: str, request: Request):
    path = "/" + full_path
    role = _target(path)
    if not role:
        return Response(content=b'{"detail":"No route"}', status_code=404,
                        media_type="application/json")
    url = f"{_base_url(role)}{path}"
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length")}
    try:
        resp = await _client.request(request.method, url, content=body, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return Response(content=f'{{"detail":"upstream {role} error: {exc}"}}'.encode(),
                        status_code=502, media_type="application/json")
    excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
    return Response(content=resp.content, status_code=resp.status_code,
                    headers=out_headers, media_type=resp.headers.get("content-type"))
