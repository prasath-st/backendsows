"""
Shared FastAPI application factory. Every microservice calls
`create_service_app(name, routers=[...])` to get a consistent app with:
  - CORS for the frontend origins
  - /health and /healthz liveness probes
  - /metrics Prometheus endpoint (scraped by Prometheus → Grafana)
  - FastAPI {detail} error shape (matches what the frontend expects)
  - graceful Redis / Kafka shutdown
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from shared.config import settings
from shared.db import close_redis_client, ping_redis
from shared.kafka_bus import close_producer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
    _PROM = True
except ModuleNotFoundError:  # pragma: no cover
    _PROM = False

if _PROM:
    REQUEST_COUNT = Counter(
        "glimmora_http_requests_total", "Total HTTP requests",
        ["service", "method", "path", "status"],
    )
    REQUEST_LATENCY = Histogram(
        "glimmora_http_request_duration_seconds", "Request latency",
        ["service", "method", "path"],
    )


def create_service_app(
    name: str,
    *,
    routers: list[APIRouter] | None = None,
    on_startup=None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("[%s] starting on port %s", name, settings.service_port)
        if on_startup is not None:
            try:
                res = on_startup()
                if hasattr(res, "__await__"):
                    await res
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] startup hook failed: %s", name, exc)
        await ping_redis()
        yield
        await close_redis_client()
        close_producer()
        logger.info("[%s] shut down", name)

    app = FastAPI(title=f"Glimmora {name}", version="1.0.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_origin_regex=r"https://.*\.vercel\.app",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if _PROM:
        import time

        @app.middleware("http")
        async def _metrics(request, call_next):
            start = time.perf_counter()
            response = await call_next(request)
            path = request.scope.get("route").path if request.scope.get("route") else request.url.path
            REQUEST_COUNT.labels(name, request.method, path, response.status_code).inc()
            REQUEST_LATENCY.labels(name, request.method, path).observe(time.perf_counter() - start)
            return response

        @app.get("/metrics", include_in_schema=False)
        def metrics():
            return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health", include_in_schema=False)
    @app.get("/healthz", include_in_schema=False)
    def health():
        return {"status": "ok", "service": name}

    for r in routers or []:
        app.include_router(r)

    return app
