"""Auth service entrypoint."""

from __future__ import annotations

import logging

from shared.app_factory import create_service_app
from shared.init_schema import init_schema

from auth_app.routers import auth
from auth_app.seed import seed_accounts

logger = logging.getLogger(__name__)


def _startup() -> None:
    try:
        init_schema()
        seed_accounts()
    except Exception as exc:  # noqa: BLE001
        logger.warning("auth-service startup tasks failed: %s", exc)


app = create_service_app("auth-service", routers=[auth.router], on_startup=_startup)
