"""SSRF guard utilities.

Provides `assert_public_url(url)` which resolves the target hostname and
raises `UnsafeUrlError` if the IP falls within any private/reserved range.

This module is the single source-of-truth for SSRF blocking across the
platform.  Both `app.integrations.mcp` and `app.core.knowledge` delegate
to it so the blocked-network list stays consistent.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    """Raised when a URL is rejected by the SSRF guard."""


_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / AWS metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),          # unique-local
    ipaddress.ip_network("fe80::/10"),         # link-local IPv6
]

_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}


def _host_is_blocked(hostname: str) -> bool:
    """Return True if *any* resolved address for hostname is private/reserved."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve host {hostname!r}") from exc
    for info in infos:
        raw = info[4][0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if any(addr in net for net in _BLOCKED_NETWORKS):
            return True
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            return True
    return False


def assert_public_url(
    url: str,
    *,
    allowed_schemes: frozenset[str] = frozenset({"https"}),
) -> str:
    """Validate *url* and return it cleaned; raise `UnsafeUrlError` otherwise.

    Checks performed:
      1. Scheme must be in *allowed_schemes* (default: only ``https``).
      2. Hostname must not be an explicitly blocked name.
      3. All resolved IP addresses must be public (not private/loopback/link-local).

    The caller is responsible for making the actual HTTP request with
    ``follow_redirects=False`` and calling this function again on any
    ``Location`` header before following a redirect.
    """
    cleaned = (url or "").strip()
    parsed = urlparse(cleaned)

    if parsed.scheme not in allowed_schemes:
        raise UnsafeUrlError(
            f"URL scheme {parsed.scheme!r} is not allowed "
            f"(allowed: {', '.join(sorted(allowed_schemes))})"
        )
    if not parsed.hostname:
        raise UnsafeUrlError("URL is missing a host")

    host = parsed.hostname.lower()
    if host in _BLOCKED_HOSTNAMES:
        raise UnsafeUrlError(f"Host {host!r} is not allowed")

    if _host_is_blocked(host):
        raise UnsafeUrlError(
            f"Host {host!r} resolves to a private or reserved address"
        )

    return cleaned
