"""
Kafka event bus — thin wrapper over kafka-python's producer. Every service
emits domain events (user.registered, sow.submitted, task.assigned, ...) for
audit, analytics, and inter-service reactions. Fail-open: if Kafka is down or
disabled, publishing is a no-op so request handling never breaks.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.config import settings

logger = logging.getLogger(__name__)

_producer: Any | None = None
_init_failed = False


def _get_producer() -> Any | None:
    global _producer, _init_failed
    if not settings.kafka_enabled or _init_failed:
        return None
    if _producer is not None:
        return _producer
    try:
        from kafka import KafkaProducer  # type: ignore

        _producer = KafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers.split(","),
            value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
            key_serializer=lambda k: (k or "").encode("utf-8"),
            acks=1,
            retries=2,
            request_timeout_ms=5000,
            api_version_auto_timeout_ms=5000,
        )
        logger.info("Kafka producer connected to %s", settings.kafka_bootstrap_servers)
        return _producer
    except Exception as exc:  # noqa: BLE001
        logger.warning("Kafka unavailable (%s); events will be dropped.", exc)
        _init_failed = True
        return None


def publish_event(topic: str, event: dict[str, Any], key: str | None = None) -> None:
    """Publish a domain event. Never raises — fail-open."""
    producer = _get_producer()
    if producer is None:
        logger.debug("Kafka disabled/unavailable — dropped event %s", topic)
        return
    try:
        producer.send(topic, key=key, value={"topic": topic, **event})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Kafka publish failed for %s: %s", topic, exc)


def close_producer() -> None:
    global _producer
    if _producer is not None:
        try:
            _producer.flush(timeout=5)
            _producer.close(timeout=5)
        except Exception:
            pass
        _producer = None
