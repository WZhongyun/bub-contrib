"""Streamed, size-capped downloads used for attachments and media."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp

from ..workspace import MAX_INBOUND_DOWNLOAD_BYTES

MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


async def download_to_path(
    session: aiohttp.ClientSession,
    url: str,
    dest: Path,
    *,
    max_bytes: int = MAX_INBOUND_DOWNLOAD_BYTES,
    headers: dict[str, str] | None = None,
    url_guard: Callable[[str], None] | None = None,
    max_redirects: int = MAX_REDIRECTS,
) -> None:
    """Stream ``url`` into ``dest``.

    With ``url_guard``, redirects are followed here instead of by aiohttp
    so the guard sees (and may reject) every hop before it is requested.
    """

    dest.parent.mkdir(parents=True, exist_ok=True)
    request: dict[str, Any] = {"headers": headers} if headers else {}
    if url_guard is not None:
        request["allow_redirects"] = False
    for _ in range(max_redirects + 1):
        if url_guard is not None:
            url_guard(url)
        async with session.get(url, **request) as response:
            if url_guard is not None and response.status in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("redirect without Location header")
                url = urljoin(str(response.url), location)
                continue
            response.raise_for_status()
            await _write_body(response, dest, max_bytes)
            return
    raise ValueError(f"more than {max_redirects} redirects")


async def _write_body(
    response: aiohttp.ClientResponse, dest: Path, max_bytes: int
) -> None:
    size = 0
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with tmp.open("wb") as handle:
            async for chunk in response.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("download exceeded size cap")
                handle.write(chunk)
        tmp.replace(dest)
    except BaseException:
        # Size cap, dropped connection, timeout or cancellation: never leave
        # a half-written .part file behind.
        tmp.unlink(missing_ok=True)
        raise
