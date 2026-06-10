"""Superadmin service entrypoint — Glimmora admin backend."""

from __future__ import annotations

import logging

from shared.app_factory import create_service_app
from shared.init_schema import init_schema

from superadmin_app.routers import audit, bulk, kyc, platform_ops, reviewer, settings, users
from superadmin_app.routers import (
    governance,
    roles,
    rubrics,
    payment_rails,
    partnerships,
    email_templates,
)
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
    # Ensure the newly-added domain tables exist + are seeded (each is fail-open).
    for init_fn in (
        getattr(governance, "init_governance_schema", None),
        getattr(roles, "init_roles_schema", None),
        getattr(rubrics, "init_rubrics_schema", None),
        getattr(payment_rails, "init_payment_rails_schema", None),
        getattr(partnerships, "init_partnerships_schema", None),
        getattr(email_templates, "init_email_templates_schema", None),
    ):
        if init_fn:
            try:
                init_fn()
            except Exception as exc:  # noqa: BLE001
                logger.warning("domain schema init failed: %s", exc)


app = create_service_app(
    "superadmin-service",
    routers=[auth_router.router, sso_router.router, users.router, settings.router, reviewer.router, bulk.router, audit.router, kyc.router, platform_ops.router, ai_agents_router, tenant_subscription_router, tenants_list_router,
             governance.router, roles.router, rubrics.router, payment_rails.router, partnerships.router, email_templates.router],
    on_startup=_startup,
)
