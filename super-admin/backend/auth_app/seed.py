"""Seed the superadmin + frontend service accounts on startup (idempotent)."""

from __future__ import annotations

import logging

from shared.config import settings
from shared.security import hash_password

from auth_app import repo

logger = logging.getLogger(__name__)


def _ensure(email: str, password: str, *, first: str, last: str, role: str) -> None:
    existing = repo.find_account_by_email(email)
    if existing:
        # Only set the seed password if the account has NO password yet (e.g. it
        # was created by another service with no hash). NEVER overwrite an
        # existing working password on every restart — that was silently resetting
        # the super-admin credential and breaking login.
        if not existing.get("password_hash"):
            try:
                repo.set_password(str(existing["id"]), hash_password(password), clear_must_change=True)
                logger.info("Set seed password for passwordless account %s (%s)", email, role)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not set seed password for %s: %s", email, exc)
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
