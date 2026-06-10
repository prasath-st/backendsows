"""
Vercel Blob storage client. Uploads/downloads files via the Vercel Blob REST
API using BLOB_READ_WRITE_TOKEN. Used by file-service and any service that
accepts file uploads (workroom uploads, profile pictures, SOW documents).

API: PUT https://blob.vercel-storage.com/{pathname}
     headers: authorization: Bearer <token>, x-api-version: 7
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from shared.config import settings

logger = logging.getLogger(__name__)

_BLOB_BASE = "https://blob.vercel-storage.com"
_API_VERSION = "7"


def blob_is_configured() -> bool:
    return bool(settings.blob_read_write_token.strip())


async def upload_blob(
    *,
    pathname: str,
    data: bytes,
    content_type: str | None = None,
    add_random_suffix: bool = True,
) -> dict[str, Any]:
    """Upload bytes to Vercel Blob. Returns the JSON descriptor incl. `url`."""
    if not blob_is_configured():
        raise RuntimeError("BLOB_READ_WRITE_TOKEN not configured")
    headers = {
        "authorization": f"Bearer {settings.blob_read_write_token}",
        "x-api-version": _API_VERSION,
        "x-add-random-suffix": "1" if add_random_suffix else "0",
    }
    if content_type:
        headers["content-type"] = content_type
    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.put(f"{_BLOB_BASE}/{pathname.lstrip('/')}", content=data, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def delete_blob(url: str) -> bool:
    """Delete a blob by its full URL."""
    if not blob_is_configured():
        return False
    headers = {
        "authorization": f"Bearer {settings.blob_read_write_token}",
        "x-api-version": _API_VERSION,
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.post(f"{_BLOB_BASE}/delete", json={"urls": [url]}, headers=headers)
        return resp.status_code < 300
