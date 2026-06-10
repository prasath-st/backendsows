"""
Shared fixtures + config for the role-backend API test suite.

Tests run against the LIVE role backends (started via backends/run_all.ps1):
  mentor 8101 · super-admin 8102 · enterprise 8103 · freelancer 8104 · reviewer 8105

Credentials come from env (override as needed):
  SA_EMAIL / SA_PASSWORD          super-admin login
  ENT_EMAIL / ENT_PASSWORD        enterprise login
They default to the accounts reset to Fayaz@123 during setup.
"""

from __future__ import annotations

import os
import pytest
import requests

TIMEOUT = float(os.environ.get("API_TEST_TIMEOUT", "15"))

BACKENDS = {
    "mentor":      "http://127.0.0.1:8101",
    "super-admin": "http://127.0.0.1:8102",
    "enterprise":  "http://127.0.0.1:8103",
    "freelancer":  "http://127.0.0.1:8104",
    "reviewer":    "http://127.0.0.1:8105",
}

CREDS = {
    "super-admin": (
        os.environ.get("SA_EMAIL", "superadmin@glimmora.dev"),
        os.environ.get("SA_PASSWORD", "Fayaz@123"),
    ),
    "enterprise": (
        os.environ.get("ENT_EMAIL", "iotcourseiot@gmail.com"),
        os.environ.get("ENT_PASSWORD", "Fayaz@123"),
    ),
}


def base(role: str) -> str:
    return BACKENDS[role]


def _login(role: str) -> str | None:
    """Return an access token for the role's login, or None if creds aren't valid."""
    creds = CREDS.get(role)
    if not creds:
        return None
    email, password = creds
    try:
        r = requests.post(
            f"{base(role)}/api/v1/auth/login",
            json={"email": email, "password": password},
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    return r.json().get("access_token")


@pytest.fixture(scope="session")
def sa_token() -> str:
    tok = _login("super-admin")
    if not tok:
        pytest.skip("super-admin login unavailable (set SA_EMAIL/SA_PASSWORD)")
    return tok


@pytest.fixture(scope="session")
def ent_token() -> str:
    tok = _login("enterprise")
    if not tok:
        pytest.skip("enterprise login unavailable (set ENT_EMAIL/ENT_PASSWORD)")
    return tok


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def get(role: str, path: str, token: str | None = None, **kw):
    headers = auth(token) if token else {}
    headers.update(kw.pop("headers", {}))
    return requests.get(f"{base(role)}{path}", headers=headers, timeout=TIMEOUT, **kw)


def post(role: str, path: str, token: str | None = None, **kw):
    headers = auth(token) if token else {}
    headers.update(kw.pop("headers", {}))
    return requests.post(f"{base(role)}{path}", headers=headers, timeout=TIMEOUT, **kw)


def delete(role: str, path: str, token: str | None = None, **kw):
    headers = auth(token) if token else {}
    headers.update(kw.pop("headers", {}))
    return requests.delete(f"{base(role)}{path}", headers=headers, timeout=TIMEOUT, **kw)


# ── Direct DB access (verify the API actually persisted to Neon) ──────────────
# DATABASE_URL is read from env; the run scripts load it from each backend's .env.
# We also fall back to reading the super-admin backend .env so DB-verify tests
# work even when pytest is launched without the env preloaded.

def _database_url() -> str | None:
    url = os.environ.get("DATABASE_URL") or os.environ.get("TEST_DATABASE_URL")
    if url:
        return url
    # fall back to the super-admin backend .env
    here = os.path.dirname(__file__)
    env_path = os.path.normpath(os.path.join(here, "..", "super-admin", "backend", ".env"))
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        return None
    return None


def _normalize_dsn(url: str) -> str:
    # psycopg2 wants a plain libpq DSN, not the SQLAlchemy +asyncpg form.
    return (url.replace("postgresql+asyncpg://", "postgresql://")
               .replace("?ssl=require", "?sslmode=require"))


@pytest.fixture(scope="session")
def db():
    """A live psycopg2 connection to the shared Neon DB, or skip if unavailable."""
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL not available for DB-verify tests")
    try:
        import psycopg2
        conn = psycopg2.connect(_normalize_dsn(url))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot connect to DB: {exc}")
    conn.autocommit = True
    yield conn
    conn.close()


def db_account(conn, email: str) -> dict | None:
    """Fetch a login_accounts row by email (dict) or None."""
    from psycopg2.extras import RealDictCursor
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, email, role, tenant_id, is_active, must_change_password, "
            "(password_hash IS NOT NULL) AS has_pw, last_login_at "
            "FROM login_accounts WHERE lower(email)=lower(%s) LIMIT 1",
            (email,),
        )
        return cur.fetchone()


def db_delete_account(conn, email: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM login_accounts WHERE lower(email)=lower(%s)", (email,))
