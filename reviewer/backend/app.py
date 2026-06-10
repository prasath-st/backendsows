"""
Standalone REVIEWER backend (per-user split).

The reviewer endpoints originated inside the super-admin service, so this app
bundles `superadmin_app` (for the reviewer router + repo + schema) but mounts
ONLY the reviewer router — it exposes no admin/users/audit endpoints. Fully
independent: depends only on its bundled code + the shared Neon DB.

Run:  uvicorn app:app --host 127.0.0.1 --port 8105
"""

from __future__ import annotations

import logging

from shared.app_factory import create_service_app
from shared.init_schema import init_schema

from superadmin_app.routers import reviewer
from superadmin_app.schema import init_superadmin_schema
from auth_app.routers import auth as auth_router

logger = logging.getLogger(__name__)


def _startup() -> None:
    try:
        init_schema()
        init_superadmin_schema()  # reviewer_assignments + reviewer_recommendations
    except Exception as exc:  # noqa: BLE001
        logger.warning("reviewer backend startup tasks failed: %s", exc)


app = create_service_app(
    "reviewer-service", routers=[auth_router.router, reviewer.router], on_startup=_startup
)
