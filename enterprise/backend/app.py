"""Enterprise service entrypoint."""

from __future__ import annotations

import logging

from shared.app_factory import create_service_app
from shared.init_schema import init_schema

from enterprise_app.db import init_enterprise_schema
from enterprise_app.routers import (
    approvals,
    billing,
    compliance_billing,
    decomp_plans,
    decomposition,
    manual_sow,
    portfolio,
    review_queue,
    sows,
    users,
    wizards,
    workforce,
)
from auth_app.routers import auth as auth_router

logger = logging.getLogger(__name__)


def _startup() -> None:
    try:
        init_schema()
        init_enterprise_schema()
    except Exception as exc:  # noqa: BLE001
        logger.warning("enterprise-service startup tasks failed: %s", exc)


app = create_service_app(
    "enterprise-service",
    routers=[
        auth_router.router,
        wizards.router,
        sows.router,
        approvals.router,
        users.router,
        manual_sow.router,
        # decomp_plans (normalised tables, with approve/activate/archive/copy) must
        # register BEFORE the legacy decomposition router so its /plans CRUD wins on
        # the shared /api/v1/enterprise/decomposition prefix — otherwise plan create
        # lands in the old JSONB table and approve/activate 404 ("Plan not found").
        decomp_plans.router,
        decomposition.router,
        decomposition.internal_router,
        portfolio.portfolio_router,
        portfolio.projects_router,
        billing.billing_router,
        billing.review_router,
        review_queue.router,
        compliance_billing.compliance_router,
        compliance_billing.rate_cards_router,
        compliance_billing.billing_export_router,
        compliance_billing.razorpay_router,
        workforce.router,
    ],
    on_startup=_startup,
)
