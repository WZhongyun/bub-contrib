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
from urllib.parse import urlparse

from aiohttp.abc import AbstractResolver
from aiohttp.abc import ResolveResult
from aiohttp.resolver import DefaultResolver


class BlockedAddressError(ValueError):
    """Raised when a download target is not a public internet address."""


def is_public_address(host: str) -> bool:
    """Whether ``host`` (an IP literal) is a globally routable address."""

    address = ipaddress.ip_address(host.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global


def check_public_url(url: str) -> None:
    """Raise :class:`BlockedAddressError` unless ``url`` may be fetched."""

    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise BlockedAddressError(f"unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise BlockedAddressError("URL has no host")
    try:
        public = is_public_address(host)
    except ValueError:
        return  # a hostname; PublicOnlyResolver checks what it resolves to
    if not public:
        raise BlockedAddressError(f"{host} is not a public internet address")


class PublicOnlyResolver(AbstractResolver):
    """DNS resolver that only returns globally routable addresses."""

    def __init__(self, inner: AbstractResolver | None = None) -> None:
        self._inner = inner if inner is not None else DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self._inner.resolve(host, port, family)
        allowed = [result for result in results if is_public_address(result["host"])]
        if not allowed:
            raise BlockedAddressError(
                f"{host} does not resolve to a public internet address"
            )
        return allowed

    async def close(self) -> None:
        await self._inner.close()
