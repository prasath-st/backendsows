"""Mentor service entrypoint."""

from __future__ import annotations

import logging

from shared.app_factory import create_service_app
from shared.init_schema import init_schema

from mentor_app.db_schema import init_mentor_schema
from mentor_app.routers import mentor

logger = logging.getLogger(__name__)


def _startup() -> None:
    try:
        init_schema()
        init_mentor_schema()
    except Exception as exc:  # noqa: BLE001
        logger.warning("mentor-service startup tasks failed: %s", exc)


app = create_service_app("mentor-service", routers=[mentor.router], on_startup=_startup)
