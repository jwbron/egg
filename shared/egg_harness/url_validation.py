"""Shared URL validation utilities for SSRF mitigation.

Provides :func:`validate_url_ssrf` which blocks requests to private networks,
cloud metadata endpoints, and other internal services.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Known-safe gateway hostnames (egg-gateway is the standard sidecar name).
_ALLOWED_GATEWAY_HOSTS: frozenset[str] = frozenset(
    {
        "egg-gateway",
        "localhost",
        "127.0.0.1",
        "::1",
    }
)

# Schemes allowed for external URLs.
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def _is_private_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address is private, reserved, loopback, or link-local.

    Also handles IPv6-mapped IPv4 addresses (e.g. ``::ffff:169.254.169.254``).
    """
    # Unwrap IPv6-mapped IPv4 addresses.
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped

    return (
        addr.is_private
        or addr.is_reserved
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
    )


def validate_url_ssrf(
    url: str,
    *,
    allow_gateway: bool = False,
    resolve_dns: bool = True,
) -> None:
    """Validate that a URL is safe from SSRF attacks.

    Blocks:
    - Non-HTTP(S) schemes
    - Private, reserved, loopback, and link-local IP addresses
    - IPv6-mapped IPv4 addresses pointing to private ranges
    - Cloud metadata endpoints (169.254.169.254, metadata.google.internal)
    - DNS resolution to private IPs (when *resolve_dns* is True)

    Args:
        url: The URL to validate.
        allow_gateway: If True, allow HTTP to known egg gateway hosts.
        resolve_dns: If True, resolve hostnames and check resulting IPs.

    Raises:
        ValueError: If the URL fails validation.
    """
    if not url:
        raise ValueError("URL must not be empty")

    parsed = urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"URL scheme {parsed.scheme!r} not allowed (must be http or https)")

    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError("URL must contain a hostname")

    # HTTPS to external hosts is always allowed.
    if parsed.scheme == "https":
        # Still check if the hostname resolves to a private IP.
        if resolve_dns:
            _validate_resolved_ips(hostname)
        return

    # HTTP: only allow known gateway hosts (when allow_gateway=True).
    if allow_gateway and hostname in _ALLOWED_GATEWAY_HOSTS:
        return

    # HTTP to an IP address: check directly.
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private_ip(addr):
            raise ValueError(
                f"URL points to private/reserved IP: {hostname}. "
                "Use HTTPS or the gateway proxy instead."
            )
        # Public IP over HTTP — allowed (the URL is reachable).
        return
    except ValueError as exc:
        if "private/reserved" in str(exc):
            raise
        # Not an IP — it's a hostname. Fall through to DNS check.

    # HTTP to a non-gateway hostname: block by default.
    if not allow_gateway:
        raise ValueError(f"HTTP to {hostname!r} is not allowed. Use HTTPS instead.")

    raise ValueError(
        f"HTTP to unknown host {hostname!r} is not allowed. "
        "Use HTTPS or route through the gateway proxy (egg-gateway)."
    )


def _validate_resolved_ips(hostname: str) -> None:
    """Resolve a hostname and check that none of the IPs are private."""
    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        # DNS resolution failed — allow the request to proceed and let
        # the HTTP client handle the connection error.
        return

    for _family, _type, _proto, _canonname, sockaddr in results:
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
            if _is_private_ip(addr):
                raise ValueError(
                    f"Hostname {hostname!r} resolves to private IP {ip_str}. "
                    "This may indicate a DNS rebinding attack."
                )
        except ValueError as exc:
            if "private IP" in str(exc) or "DNS rebinding" in str(exc):
                raise
