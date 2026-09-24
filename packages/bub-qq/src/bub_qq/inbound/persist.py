"""Save inbound QQ attachments under the workspace inbox."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
from loguru import logger

from ..protocol.models import QQAttachment
from ..workspace import MAX_INBOUND_DOWNLOAD_BYTES
from ..workspace import inbox_dir
from ..workspace import safe_filename

MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


async def persist_inbound_attachments(
    content: str,
    attachments: tuple[QQAttachment, ...],
    *,
    workspace: Path,
    message_id: str,
) -> str:
    """Download attachment URLs into ``inbox/<message_id>/`` and patch JSON.

    Failures leave the original URL-only payload in place.
    """

    if not attachments:
        return content
    try:
        payload = json.loads(content)
    except ValueError:
        return content
    if not isinstance(payload, dict):
        return content
    listed = payload.get("attachments")
    if not isinstance(listed, list):
        return content

    dest_root = inbox_dir(workspace, message_id)
    updated: list[Any] = []
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for index, (attachment, item) in enumerate(zip(attachments, listed, strict=False)):
            row = dict(item) if isinstance(item, dict) else {}
            url = attachment.url or (str(row.get("url") or "") or None)
            if not url:
                updated.append(row or item)
                continue
            if attachment.size is not None and attachment.size > MAX_INBOUND_DOWNLOAD_BYTES:
                logger.warning(
                    "qq.inbox.skip message_id={} reason=too_large size={}",
                    message_id,
                    attachment.size,
                )
                updated.append(row or item)
                continue
            filename = safe_filename(attachment.filename, url, index)
            dest = dest_root / filename
            try:
                await download_to_path(session, url, dest)
            except (OSError, aiohttp.ClientError, TimeoutError, ValueError) as exc:
                logger.warning(
                    "qq.inbox.download_failed message_id={} url={} error={}",
                    message_id,
                    url,
                    exc,
                )
                updated.append(row or item)
                continue
            row["url"] = url
            row["local_path"] = str(dest)
            updated.append(row)

    while len(updated) < len(listed):
        updated.append(listed[len(updated)])
    payload["attachments"] = updated
    return json.dumps(payload, ensure_ascii=False)


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
    with tmp.open("wb") as handle:
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                handle.close()
                tmp.unlink(missing_ok=True)
                raise ValueError("download exceeded size cap")
            handle.write(chunk)
    tmp.replace(dest)
