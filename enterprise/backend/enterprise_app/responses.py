"""Standard response envelope helpers for the enterprise service."""

from __future__ import annotations

from typing import Any


def ok(data: Any = None, message: str | None = None) -> dict:
    """The wrapped envelope most enterprise/sow/decomposition endpoints use."""
    return {"success": True, "message": message, "data": data}
