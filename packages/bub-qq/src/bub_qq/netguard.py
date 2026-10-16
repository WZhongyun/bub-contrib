"""Keep plugin-side downloads on the public internet.

``qq.send media_url`` makes the plugin fetch a URL chosen by the model,
and any group member can steer the model. Without a guard that is a
server-side request forgery: loopback services, the private network and
cloud metadata (169.254.169.254) would be fetched and posted to the chat.

Two layers are needed because aiohttp skips the resolver for IP literals:
:func:`check_public_url` rejects literal hosts on every redirect hop, and
:class:`PublicOnlyResolver` drops non-public addresses at DNS resolution
time, so the connection goes to the address that was checked (no DNS
rebinding window).
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin
from urllib.parse import urlparse

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.abc import ResolveResult
from aiohttp.resolver import DefaultResolver


MAX_FETCH_BYTES = 5 * 1024 * 1024


class BlockedAddressError(ValueError):
    """Raised when a download target is not a public internet address."""


def is_public_address(host: str) -> bool:
    """Whether ``host`` (an IP literal) is a globally routable address."""

    address = ipaddress.ip_address(host.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global


def check_public_url(url: str, *, allow_hosts: frozenset[str] = frozenset()) -> None:
    """Raise :class:`BlockedAddressError` unless ``url`` may be fetched.

    ``allow_hosts`` (``download_allow_hosts``) exempts listed hostnames.
    """

    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise BlockedAddressError(f"unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise BlockedAddressError("URL has no host")
    if host.lower() in allow_hosts:
        return
    try:
        public = is_public_address(host)
    except ValueError:
        return  # a hostname; PublicOnlyResolver checks what it resolves to
    if not public:
        raise BlockedAddressError(f"{host} is not a public internet address")


class PublicOnlyResolver(AbstractResolver):
    """DNS resolver that only returns globally routable addresses."""

    def __init__(
        self,
        inner: AbstractResolver | None = None,
        *,
        allow_hosts: frozenset[str] = frozenset(),
    ) -> None:
        self._inner = inner if inner is not None else DefaultResolver()
        self._allow_hosts = allow_hosts

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self._inner.resolve(host, port, family)
        if host.lower() in self._allow_hosts:
            return results
        allowed = [result for result in results if is_public_address(result["host"])]
        if not allowed:
            raise BlockedAddressError(
                f"{host} does not resolve to a public internet address"
            )
        return allowed

    async def close(self) -> None:
        await self._inner.close()


def guarded_session(
    *, timeout: float, allow_hosts: frozenset[str] = frozenset(), **kwargs: Any
) -> aiohttp.ClientSession:
    """A client session whose connections only reach public addresses."""

    connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver(allow_hosts=allow_hosts))
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout), connector=connector, **kwargs
    )


async def fetch_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    max_bytes: int = MAX_FETCH_BYTES,
    allow_hosts: frozenset[str] = frozenset(),
    max_redirects: int = 5,
) -> str:
    """GET ``url`` like Bub's ``web.fetch``, but only on the public internet.

    Every redirect hop is checked before it is requested, and the body is
    capped at ``max_bytes``.
    """

    async with guarded_session(
        timeout=timeout, allow_hosts=allow_hosts, headers=headers or {}
    ) as session:
        for _ in range(max_redirects + 1):
            check_public_url(url, allow_hosts=allow_hosts)
            async with session.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("redirect without Location header")
                    url = urljoin(str(response.url), location)
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError(f"response larger than {max_bytes} bytes")
                return bytes(body).decode(response.charset or "utf-8", "replace")
        raise ValueError(f"more than {max_redirects} redirects")


def download_allow_hosts() -> frozenset[str]:
    """Lower-cased ``download_allow_hosts`` from the QQ config."""

    import bub

    from .config import QQConfig
    from .security import parse_id_list

    config = bub.ensure_config(QQConfig)
    return frozenset(host.lower() for host in parse_id_list(config.download_allow_hosts or ""))


async def web_fetch_for_call(arguments: dict[str, Any] | None) -> tuple[bool, str]:
    """Run a ``web.fetch`` call's arguments through :func:`fetch_text`.

    Returns ``(ok, text)``: the page text, or the reason it was refused or
    failed. Bub's own handler follows redirects to any address, so both the
    model's tool call and an admin's ``,web.fetch`` command come here.
    """

    args = arguments if isinstance(arguments, dict) else {}
    url = str(args.get("url") or "")
    headers = args.get("headers") if isinstance(args.get("headers"), dict) else {}
    timeout = args.get("timeout")
    try:
        timeout_seconds = float(timeout) if timeout not in (None, "") else 30.0
    except (TypeError, ValueError):
        timeout_seconds = 30.0
    if timeout_seconds <= 0:
        timeout_seconds = 30.0
    try:
        text = await fetch_text(
            url,
            headers={str(k): str(v) for k, v in headers.items()},
            timeout=timeout_seconds,
            allow_hosts=download_allow_hosts(),
        )
    except BlockedAddressError as exc:
        return False, f"web.fetch refused: {exc}"
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        return False, f"web.fetch failed: {exc}"
    return True, text
