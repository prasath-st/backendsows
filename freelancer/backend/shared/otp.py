"""
Redis-backed OTP store + rate limiting. OTP codes live in Redis with a TTL.
When Redis is unavailable, a dev fallback returns the code in the response
(matches the frontend's existing dev-OTP behaviour).
"""

from __future__ import annotations

import logging
import secrets
import time

from shared.config import settings
from shared.db import get_redis_client

logger = logging.getLogger(__name__)

OTP_TTL_SECONDS = 5 * 60

# In-process fallback store used when Redis is unavailable (e.g. running the
# backend directly without Docker). Keeps OTP send/verify + the verified marker
# working locally. Maps key -> (value, expires_at_epoch).
_MEM: dict[str, tuple[str, float]] = {}


def _mem_set(key: str, value: str, ttl: int) -> None:
    _MEM[key] = (value, time.time() + ttl)


def _mem_get(key: str) -> str | None:
    item = _MEM.get(key)
    if not item:
        return None
    value, exp = item
    if time.time() > exp:
        _MEM.pop(key, None)
        return None
    return value


def _mem_del(key: str) -> None:
    _MEM.pop(key, None)


def generate_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


def _key(channel: str, identifier: str) -> str:
    return f"glimmora:otp:{channel}:{identifier.lower()}"


async def store_otp(channel: str, identifier: str, code: str) -> bool:
    """Store an OTP code. Returns True if persisted to Redis, False if dev-fallback."""
    client = get_redis_client()
    if client is None:
        _mem_set(_key(channel, identifier), code, OTP_TTL_SECONDS)
        logger.info("[OTP] Redis unavailable — using in-process store for %s:%s", channel, identifier)
        return True
    try:
        await client.set(_key(channel, identifier), code, ex=OTP_TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OTP] store failed: %s", exc)
        return False


# How long a successful email verification is "remembered" so a subsequent
# registration (which happens just after) can confirm the email was verified
# even though no account row exists yet.
VERIFIED_TTL_SECONDS = 30 * 60


def _verified_key(channel: str, identifier: str) -> str:
    return f"glimmora:otp-verified:{channel}:{identifier.lower()}"


async def verify_otp(channel: str, identifier: str, code: str) -> bool:
    client = get_redis_client()
    if client is None:
        stored = _mem_get(_key(channel, identifier))
        if stored and str(stored) == str(code):
            _mem_del(_key(channel, identifier))
            _mem_set(_verified_key(channel, identifier), "1", VERIFIED_TTL_SECONDS)
            return True
        return False
    try:
        stored = await client.get(_key(channel, identifier))
        if stored and str(stored) == str(code):
            await client.delete(_key(channel, identifier))
            # Remember the verification briefly for pre-account flows (signup).
            try:
                await client.set(_verified_key(channel, identifier), "1", ex=VERIFIED_TTL_SECONDS)
            except Exception:  # noqa: BLE001
                pass
            return True
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OTP] verify failed: %s", exc)
        return False


async def is_recently_verified(channel: str, identifier: str) -> bool:
    """True if this identifier completed OTP verification within the TTL window.
    Used at registration to enforce 'must verify email before submitting'.
    Fail-closed when Redis is unavailable (so we never silently skip the gate)."""
    client = get_redis_client()
    if client is None:
        return _mem_get(_verified_key(channel, identifier)) is not None
    try:
        return bool(await client.get(_verified_key(channel, identifier)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OTP] verified-check failed: %s", exc)
        return False


async def rate_limit_ok(bucket: str, identifier: str, limit: int, window_seconds: int) -> bool:
    """Sliding-ish fixed-window limiter. Fail-open if Redis is down."""
    if not settings.rate_limit_enabled:
        return True
    client = get_redis_client()
    if client is None:
        return True
    key = f"{settings.rate_limit_key_prefix}:{bucket}:{identifier.lower()}"
    try:
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, window_seconds)
        return count <= limit
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ratelimit] failed open: %s", exc)
        return True
