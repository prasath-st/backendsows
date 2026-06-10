"""
Shared DB connections — Neon Postgres (psycopg2), MongoDB Atlas (pymongo),
Redis. Connection-pool-lite with reconnect-on-drop for Neon's idle cutoff.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import psycopg2
from pymongo import MongoClient
from pymongo.database import Database
from pymongo.server_api import ServerApi

try:
    from redis.asyncio import Redis
except ModuleNotFoundError:  # pragma: no cover
    Redis = Any  # type: ignore[misc,assignment]

from shared.config import settings

logger = logging.getLogger(__name__)

_pg_conn: psycopg2.extensions.connection | None = None
_mongo_client: MongoClient | None = None
_redis_client: "Redis | None" = None


# ── PostgreSQL ───────────────────────────────────────────────────────────────

def _is_pg_alive(conn: psycopg2.extensions.connection) -> bool:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except psycopg2.Error:
        return False


def _connect_with_retry(attempts: int = 5, connect_timeout: int = 30):
    """Open a Postgres connection; retry if Neon's compute is cold-starting."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return psycopg2.connect(settings.postgres_dsn, connect_timeout=connect_timeout)
        except psycopg2.OperationalError as exc:
            last_exc = exc
            logger.warning("PG connect attempt %d/%d failed: %s", i + 1, attempts, str(exc).splitlines()[0])
            time.sleep(2 * (i + 1))
    assert last_exc is not None
    raise last_exc


def get_pg_connection() -> psycopg2.extensions.connection:
    """Return a live Postgres connection; reconnect if Neon dropped it."""
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed or not _is_pg_alive(_pg_conn):
        if _pg_conn is not None and not _pg_conn.closed:
            try:
                _pg_conn.close()
            except Exception:
                pass
        logger.info("Opening new PostgreSQL connection to Neon")
        _pg_conn = _connect_with_retry()
    return _pg_conn


def prepare_pg_connection() -> None:
    """Roll back an aborted transaction so subsequent queries don't fail."""
    global _pg_conn
    if _pg_conn is None or _pg_conn.closed:
        return
    try:
        _pg_conn.rollback()
    except psycopg2.Error:
        try:
            _pg_conn.close()
        except Exception:
            pass
        _pg_conn = None


def ensure_pg_clean() -> None:
    prepare_pg_connection()
    get_pg_connection()


# ── MongoDB ──────────────────────────────────────────────────────────────────

def get_mongo_db() -> Database | None:
    global _mongo_client
    if not settings.mongodb_uri.strip():
        return None
    if _mongo_client is None:
        # Short timeouts so an unreachable Mongo fails fast (audit is fail-open)
        # instead of hanging requests for the 20-30s pymongo default.
        _mongo_client = MongoClient(
            settings.mongodb_uri, server_api=ServerApi("1"),
            serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=3000,
        )
    return _mongo_client[settings.mongodb_db]


def mongo_audit_collection() -> Any | None:
    db = get_mongo_db()
    if db is None:
        return None
    return db["audit_log"]


# ── Redis ────────────────────────────────────────────────────────────────────

def get_redis_client() -> "Redis | None":
    """Lazily-created Redis client for OTP, rate limits, sessions."""
    global _redis_client
    if not settings.redis_is_configured:
        return None
    if not hasattr(Redis, "from_url"):
        logger.warning("redis package not installed; Redis features disabled.")
        return None
    if _redis_client is None:
        _redis_client = Redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
    return _redis_client


async def ping_redis() -> bool:
    client = get_redis_client()
    if client is None:
        return False
    try:
        await client.ping()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis ping failed: %s", exc)
        return False


async def close_redis_client() -> None:
    global _redis_client
    if _redis_client is None:
        return
    try:
        close_fn = getattr(_redis_client, "aclose", None) or getattr(_redis_client, "close", None)
        if close_fn is not None:
            result = close_fn()
            if result is not None:
                await result
    except Exception as exc:  # noqa: BLE001
        logger.debug("Redis close failed: %s", exc)
    finally:
        _redis_client = None
