"""
Shared settings for all Glimmora microservices. Reads the single
`backend/.env` file (full connection URLs for Neon / Mongo / Redis).

Each service sets SERVICE_NAME / SERVICE_PORT in its own environment; the
rest is shared so JWTs interoperate across services (same API_SECRET_KEY).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # ── Service identity (overridden per service via env) ────────────────────
    service_name: str = "glimmora-service"
    service_port: int = 9000

    # ── PostgreSQL (Neon) — full connection URL ──────────────────────────────
    # Async URL for app (asyncpg) and sync URL for psycopg2 / migrations.
    database_url: str = ""
    database_url_sync: str = ""

    @property
    def postgres_dsn(self) -> str:
        """psycopg2-compatible DSN. Prefer the sync URL; fall back to the
        async URL with the +asyncpg driver suffix stripped."""
        url = self.database_url_sync or self.database_url
        # Normalise SQLAlchemy-style URLs to plain libpq DSNs for psycopg2.
        url = url.replace("postgresql+asyncpg://", "postgresql://")
        url = url.replace("postgresql+psycopg2://", "postgresql://")
        # asyncpg uses ssl=require; libpq wants sslmode=require.
        url = url.replace("ssl=require", "sslmode=require")
        return url

    # ── MongoDB — audit logs + flexible documents ────────────────────────────
    mongodb_uri: str = ""
    mongodb_db: str = "glimmora"

    # ── Redis — OTP / sessions / rate-limit ──────────────────────────────────
    redis_url: str = ""

    @property
    def redis_is_configured(self) -> bool:
        return bool(self.redis_url.strip())

    # ── JWT / Auth (shared across all services) ───────────────────────────────
    api_secret_key: str = "change-me-to-a-long-random-string-for-jwt"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24
    refresh_token_expire_minutes: int = 60 * 24 * 30

    # ── Rate limiting ─────────────────────────────────────────────────────────
    rate_limit_enabled: bool = True
    rate_limit_login_requests: int = 30
    rate_limit_login_window_seconds: int = 60
    rate_limit_api_requests: int = 600
    rate_limit_api_window_seconds: int = 60
    rate_limit_key_prefix: str = "glimmora:ratelimit"

    # ── Email / SMTP (Gmail) ──────────────────────────────────────────────────
    # EMAIL_ENABLED=false on cloud deploys where outbound SMTP (port 587) is
    # blocked — keeps OTP / credential flows fast (they return a dev code instead
    # of hanging on an unreachable mail server).
    email_enabled: bool = True
    email_user: str = ""
    email_app_password: str = ""
    email_from: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_use_tls: bool = True

    @property
    def email_app_password_for_smtp(self) -> str:
        return self.email_app_password.replace(" ", "")

    @property
    def email_from_address(self) -> str:
        return (self.email_from or self.email_user).strip()

    # ── Kafka event bus ───────────────────────────────────────────────────────
    # Off by default: cloud deploys (Render/Vercel) have no broker, and events are
    # fail-open (dropped). Set KAFKA_ENABLED=true + KAFKA_BOOTSTRAP_SERVERS to use.
    kafka_enabled: bool = False
    kafka_bootstrap_servers: str = "kafka:9092"

    # ── Super admin (the Glimmora admin) ──────────────────────────────────────
    super_admin_email: str = "superadmin@glimmora.dev"
    super_admin_password: str = "glimmora123"
    dev_default_portal_password: str = "glimmora123"

    # ── Service-account logins used by the frontend proxy routes ──────────────
    glimmora_service_email: str = "sow-test-user@glimmora.com"
    glimmora_service_password: str = "Test@12345"
    glimmora_enterprise_service_email: str = "enterprise-service@glimmora.com"
    glimmora_enterprise_service_password: str = "Test@12345"

    # ── CORS ───────────────────────────────────────────────────────────────────
    cors_origins: str = (
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:3100,http://127.0.0.1:3100"
    )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # ── Vercel Blob — file storage ────────────────────────────────────────────
    blob_read_write_token: str = ""

    # ── OAuth (contributors/freelancers only) ─────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant_id: str = "common"
    oauth_public_base_url: str = "http://localhost:9000"

    # ── Public gateway base ───────────────────────────────────────────────────
    public_api_base_url: str = "http://localhost:9000"
    frontend_base_url: str = "http://localhost:3000"


settings = Settings()
