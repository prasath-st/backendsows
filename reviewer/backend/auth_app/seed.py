"""Seed the superadmin + frontend service accounts on startup (idempotent)."""

from __future__ import annotations

import logging

from shared.config import settings
from shared.security import hash_password

from auth_app import repo

logger = logging.getLogger(__name__)


def _ensure(email: str, password: str, *, first: str, last: str, role: str) -> None:
    if repo.find_account_by_email(email):
        return
    repo.create_account(
        email=email, password_hash=hash_password(password),
        first_name=first, last_name=last, role=role,
        provider="password", email_verified=True,
    )
    logger.info("Seeded account %s (%s)", email, role)


def seed_accounts() -> None:
    # The Glimmora super admin.
    _ensure(settings.super_admin_email, settings.super_admin_password,
            first="Super", last="Admin", role="superadmin")
    # Service accounts the frontend proxy routes log in as.
    _ensure(settings.glimmora_service_email, settings.glimmora_service_password,
            first="SOW", last="Service", role="contributor")
    _ensure(settings.glimmora_enterprise_service_email, settings.glimmora_enterprise_service_password,
            first="Enterprise", last="Service", role="enterprise")
