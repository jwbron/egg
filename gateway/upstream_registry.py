"""
Upstream Registry for Gateway LLM Proxy.

Provides a per-upstream registry pairing an httpx.Client (base_url, timeout,
connection limits) with a credential resolver. Used by the
``/v1/messages`` and ``/v1/messages/count_tokens`` proxy routes to select
between today's Anthropic upstream and a future LiteLLM-translation proxy
in ``egg-system``.

The registry is the gateway-side trust boundary for upstream selection:
the orchestrator declares the per-agent upstream at session-create time
(``Session.upstream``), and the proxy route resolves the registry entry
per request before injecting credentials and forwarding bytes upstream.

With no agent configured for LiteLLM, no LiteLLM-bound request ever fires
— ``UpstreamRegistry`` is the seam, not a behavior change. See cq-1 and
cq-7 on issue #2769.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import httpx

# Add shared directory to path for egg_logging
_shared_path = Path(__file__).parent.parent / "shared"
if _shared_path.exists():
    sys.path.insert(0, str(_shared_path))
from egg_logging import get_logger

# Import gateway modules - try relative import first (module mode),
# fall back to absolute import (standalone / pytest load path). Mirrors the
# pattern used by gateway/gateway.py so this module is importable both as
# ``gateway.upstream_registry`` and as a top-level ``upstream_registry``.
try:
    from .anthropic_credentials import (
        AnthropicCredential,
        LiteLLMCredentialsManager,
        get_credentials_manager,
        get_litellm_credentials_manager,
    )
except ImportError:  # pragma: no cover - exercised by standalone import paths
    _gateway_dir = str(Path(__file__).parent)
    if _gateway_dir not in sys.path:
        sys.path.insert(0, _gateway_dir)
    from anthropic_credentials import (  # type: ignore[no-redef]
        AnthropicCredential,
        LiteLLMCredentialsManager,
        get_credentials_manager,
        get_litellm_credentials_manager,
    )

logger = get_logger("gateway.upstream-registry")


# Anthropic upstream base URL — matches today's hard-wired client.
ANTHROPIC_BASE_URL = "https://api.anthropic.com"  # noqa: EGG200 - proxy target URL, not a direct LLM call

# LiteLLM proxy Service DNS — overridable via env var so operators can
# point at a different proxy without rebuilding the gateway image.
LITELLM_BASE_URL_DEFAULT = "http://litellm.egg-system.svc.cluster.local:4000"

# The upstream names this registry serves. Single source of truth for
# ``get`` / ``is_known`` / ``known_upstreams``; adding a fourth upstream
# means adding a name here and an ``_ensure_<name>`` constructor in ``get``.
KNOWN_UPSTREAMS: tuple[str, ...] = ("anthropic", "litellm")


# Type alias for the credential resolver shape — both anthropic and litellm
# resolvers return ``AnthropicCredential | None``. Reusing the dataclass keeps
# the credential-injection code path uniform (header_name / header_value).
CredentialResolver = Callable[[], "AnthropicCredential | None"]


class UnknownUpstreamError(KeyError):
    """Raised when ``UpstreamRegistry.get`` is called with an unregistered name."""


class UpstreamRegistry:
    """Per-upstream registry of (httpx.Client, credential_resolver) pairs.

    Each entry is created lazily on first ``get(name)``. The clients share the
    same timeout / connection-pool characteristics as today's
    ``_anthropic_client`` so behavior is byte-identical on the Anthropic path.
    """

    def __init__(
        self,
        litellm_base_url: str | None = None,
    ) -> None:
        self._litellm_base_url = litellm_base_url or os.environ.get(
            "LITELLM_BASE_URL", LITELLM_BASE_URL_DEFAULT
        )
        self._clients: dict[str, httpx.Client] = {}
        self._resolvers: dict[str, CredentialResolver] = {}
        self._lock = threading.Lock()

    def _make_client(self, base_url: str) -> httpx.Client:
        """Create an httpx.Client with the same shape as today's singleton."""
        return httpx.Client(
            base_url=base_url,  # noqa: EGG200 - gateway proxy client, not direct LLM call
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    def _ensure_anthropic(self) -> None:
        if "anthropic" in self._clients:
            return
        self._clients["anthropic"] = self._make_client(ANTHROPIC_BASE_URL)
        # The anthropic resolver wraps the existing global
        # AnthropicCredentialsManager so its mtime-invalidated cache is shared
        # with any direct callers of get_credentials_manager().
        self._resolvers["anthropic"] = lambda: get_credentials_manager().get_credential()

    def _ensure_litellm(self) -> None:
        if "litellm" in self._clients:
            return
        self._clients["litellm"] = self._make_client(self._litellm_base_url)
        self._resolvers["litellm"] = lambda: get_litellm_credentials_manager().get_credential()

    def get(self, upstream: str) -> tuple[httpx.Client, CredentialResolver]:
        """Return ``(client, credential_resolver)`` for ``upstream``.

        Raises ``UnknownUpstreamError`` if ``upstream`` is not a registered
        name. Registration is implicit on first call for the canonical
        upstreams (``anthropic`` and ``litellm``) — both share construction
        semantics with today's ``get_anthropic_client()``.
        """
        if upstream not in KNOWN_UPSTREAMS:
            raise UnknownUpstreamError(upstream)
        with self._lock:
            if upstream == "anthropic":
                self._ensure_anthropic()
            elif upstream == "litellm":
                self._ensure_litellm()

            return self._clients[upstream], self._resolvers[upstream]

    def is_known(self, upstream: str) -> bool:
        """Return True if ``upstream`` is a name the registry will serve."""
        return upstream in KNOWN_UPSTREAMS

    def known_upstreams(self) -> tuple[str, ...]:
        """Return the canonical upstream names the registry will serve."""
        return KNOWN_UPSTREAMS

    def close(self) -> None:
        """Close all open httpx clients. For tests / teardown."""
        with self._lock:
            for client in self._clients.values():
                try:
                    client.close()
                except Exception:
                    logger.debug("Failed to close httpx client")
            self._clients.clear()
            self._resolvers.clear()


# Module-level singleton mirrors today's ``_anthropic_client`` lifetime.
_upstream_registry: UpstreamRegistry | None = None
_registry_lock = threading.Lock()


def get_upstream_registry() -> UpstreamRegistry:
    """Return the module-level ``UpstreamRegistry`` singleton."""
    global _upstream_registry
    if _upstream_registry is None:
        with _registry_lock:
            if _upstream_registry is None:
                _upstream_registry = UpstreamRegistry()
    return _upstream_registry


def reset_upstream_registry() -> None:
    """Reset the module-level registry. For tests only."""
    global _upstream_registry
    with _registry_lock:
        if _upstream_registry is not None:
            try:
                _upstream_registry.close()
            except Exception:
                pass
        _upstream_registry = None


__all__ = [
    "ANTHROPIC_BASE_URL",
    "KNOWN_UPSTREAMS",
    "LITELLM_BASE_URL_DEFAULT",
    "CredentialResolver",
    "LiteLLMCredentialsManager",
    "UnknownUpstreamError",
    "UpstreamRegistry",
    "get_upstream_registry",
    "reset_upstream_registry",
]
