"""Superadmin service entrypoint — Glimmora admin backend."""

from __future__ import annotations

import logging

from shared.app_factory import create_service_app
from shared.init_schema import init_schema

from superadmin_app.routers import audit, bulk, kyc, platform_ops, reviewer, settings, users
from superadmin_app.routers.ai_agents import router as ai_agents_router
from superadmin_app.routers.tenant_subscription import router as tenant_subscription_router
from superadmin_app.routers.tenants_list import router as tenants_list_router
from superadmin_app.schema import init_superadmin_schema
from auth_app.routers import auth as auth_router
from auth_app.routers import sso as sso_router

logger = logging.getLogger(__name__)


def _startup() -> None:
    try:
        init_schema()
        init_superadmin_schema()
    except Exception as exc:  # noqa: BLE001
        logger.warning("superadmin-service startup tasks failed: %s", exc)


app = create_service_app(
    "superadmin-service",
    routers=[auth_router.router, sso_router.router, users.router, settings.router, reviewer.router, bulk.router, audit.router, kyc.router, platform_ops.router, ai_agents_router, tenant_subscription_router, tenants_list_router],
    on_startup=_startup,
)
