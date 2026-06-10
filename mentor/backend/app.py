"""
Standalone MENTOR backend — runs the mentor service on its own (per-user split).

This is a fully independent FastAPI app: it depends only on its bundled
`mentor_app/` + `shared/` and the shared Neon database. It does NOT import any
other role's service.

Run:  uvicorn app:app --host 127.0.0.1 --port 8101
"""

from __future__ import annotations

import logging

from shared.app_factory import create_service_app
from shared.init_schema import init_schema

from mentor_app.db_schema import init_mentor_schema
from mentor_app.routers import mentor
from mentor_app.routers import notes_sessions
from mentor_app.routers import v1 as mentor_v1
from auth_app.routers import auth as auth_router
from auth_app.routers.sessions import router as sessions_router, auth_sessions_router

logger = logging.getLogger(__name__)


def _startup() -> None:
    try:
        init_schema()
        init_mentor_schema()
    except Exception as exc:  # noqa: BLE001
        logger.warning("mentor backend startup tasks failed: %s", exc)


app = create_service_app(
    "mentor-service",
    routers=[
        auth_router.router,
        mentor.router,
        notes_sessions.router,
        mentor_v1.router,
        sessions_router,
        auth_sessions_router,
    ],
    on_startup=_startup,
)
