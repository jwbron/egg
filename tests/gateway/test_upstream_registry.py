"""Tests for the gateway UpstreamRegistry (issue #2769 slice-1).

The registry replaces the lone ``get_anthropic_client()`` singleton at
``gateway/gateway.py:9320`` with a per-upstream-name lookup
(``"anthropic"`` -> the Anthropic httpx client + Anthropic credential
resolver, ``"litellm"`` -> the LiteLLM client + LiteLLM credential
resolver).  Slice 1 wires it through ``proxy_anthropic_messages`` and
``proxy_count_tokens`` but no agent yet asks for ``"litellm"``, so the
registry is exercised primarily by these unit tests in slice 1.

Coverage targets (from plan slice 1 acceptance criteria for TASK-1-1):

- ``UpstreamRegistry.get("anthropic")`` returns a client with
  ``base_url == "https://api.anthropic.com"`` and the existing
  Anthropic credential resolver.
- ``UpstreamRegistry.get("litellm")`` returns a client whose
  ``base_url`` is sourced from ``LITELLM_BASE_URL`` (default
  ``http://litellm.egg-system.svc.cluster.local:4000``) and the
  LiteLLM credential resolver.
- ``UpstreamRegistry.get("unknown")`` raises ``UnknownUpstreamError``.
- Both clients share the same timeout / pooling characteristics as
  today's ``_anthropic_client`` (regression guard for the singleton's
  semantics).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import httpx
import pytest

# Add gateway to path for imports — mirror the pattern used by
# tests/gateway/test_anthropic_credentials.py / test_anthropic_proxy.py.
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "gateway"))


@pytest.fixture(autouse=True)
def _reset_upstream_registry(monkeypatch):
    """Reset the global registry between tests so env-var-driven config
    changes (LITELLM_BASE_URL) are observed.

    The registry is expected to expose ``reset_upstream_registry()`` for
    test isolation, mirroring ``reset_credentials_manager()`` at
    ``gateway/anthropic_credentials.py:226``.
    """
    try:
        from upstream_registry import reset_upstream_registry  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover — coder will land the module
        pytest.skip("upstream_registry not yet implemented (waiting on coder commit)")
    reset_upstream_registry()
    yield
    reset_upstream_registry()


class TestUpstreamRegistryAnthropic:
    """``UpstreamRegistry.get("anthropic")`` keeps today's behavior."""

    def test_anthropic_returns_client_with_anthropic_base_url(self):
        from upstream_registry import get_upstream_registry  # type: ignore[import-not-found]

        registry = get_upstream_registry()
        client, _credential_resolver = registry.get("anthropic")
        assert isinstance(client, httpx.Client)
        # noqa: EGG200 annotation in production code permits the literal —
        # tests assert it explicitly so a future refactor cannot silently
        # change the Anthropic upstream URL.
        assert str(client.base_url).rstrip("/") == "https://api.anthropic.com"

    def test_anthropic_credential_resolver_is_anthropic(self):
        """Anthropic upstream uses the existing AnthropicCredentialsManager."""
        from anthropic_credentials import AnthropicCredentialsManager
        from upstream_registry import get_upstream_registry  # type: ignore[import-not-found]

        registry = get_upstream_registry()
        _client, credential_resolver = registry.get("anthropic")
        # Resolver is callable / returns an AnthropicCredential or None;
        # the resolver instance itself MUST come from the Anthropic
        # credentials manager — not the LiteLLM one — so existing
        # ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN behavior is
        # untouched.
        manager = getattr(credential_resolver, "__self__", None)
        assert manager is None or isinstance(manager, AnthropicCredentialsManager), (
            "Anthropic upstream credential resolver must be backed by AnthropicCredentialsManager"
        )


class TestUpstreamRegistryLiteLLM:
    """``UpstreamRegistry.get("litellm")`` returns a fresh LiteLLM client."""

    def test_litellm_default_base_url(self, monkeypatch):
        """When LITELLM_BASE_URL is unset, defaults to the cluster Service DNS."""
        monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
        # Force re-import so the module-level default re-reads the env.
        from upstream_registry import (  # type: ignore[import-not-found]
            get_upstream_registry,
            reset_upstream_registry,
        )

        reset_upstream_registry()
        registry = get_upstream_registry()
        client, _credential_resolver = registry.get("litellm")
        assert isinstance(client, httpx.Client)
        assert str(client.base_url).rstrip("/") == (
            "http://litellm.egg-system.svc.cluster.local:4000"
        )

    def test_litellm_custom_base_url_from_env(self, monkeypatch):
        """LITELLM_BASE_URL overrides the default."""
        monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm-custom:5555")
        # Re-import upstream_registry to pick up new env var
        if "upstream_registry" in sys.modules:
            importlib.reload(sys.modules["upstream_registry"])
        from upstream_registry import (  # type: ignore[import-not-found]
            get_upstream_registry,
            reset_upstream_registry,
        )

        reset_upstream_registry()
        registry = get_upstream_registry()
        client, _credential_resolver = registry.get("litellm")
        assert str(client.base_url).rstrip("/") == "http://litellm-custom:5555"

    def test_litellm_credential_resolver_is_litellm(self):
        """LiteLLM upstream uses the LiteLLM credential resolver, not Anthropic."""
        from anthropic_credentials import AnthropicCredentialsManager
        from upstream_registry import get_upstream_registry  # type: ignore[import-not-found]

        registry = get_upstream_registry()
        _client, credential_resolver = registry.get("litellm")
        manager = getattr(credential_resolver, "__self__", None)
        assert manager is None or not isinstance(manager, AnthropicCredentialsManager), (
            "LiteLLM upstream credential resolver must NOT be the AnthropicCredentialsManager"
        )


class TestUpstreamRegistryUnknown:
    """Unknown upstream names raise a typed error."""

    def test_unknown_upstream_raises_typed_error(self):
        from upstream_registry import (  # type: ignore[import-not-found]
            UnknownUpstreamError,
            get_upstream_registry,
        )

        registry = get_upstream_registry()
        with pytest.raises(UnknownUpstreamError):
            registry.get("unknown")

    def test_unknown_upstream_error_names_the_upstream(self):
        """The error message should name the offending upstream so the
        gateway operator can debug a misconfigured session quickly."""
        from upstream_registry import (  # type: ignore[import-not-found]
            UnknownUpstreamError,
            get_upstream_registry,
        )

        registry = get_upstream_registry()
        with pytest.raises(UnknownUpstreamError) as exc_info:
            registry.get("bogus_upstream_name")
        assert "bogus_upstream_name" in str(exc_info.value)


class TestUpstreamRegistryClientSemantics:
    """Both upstream clients share today's singleton's timeout / pooling
    characteristics so neither regresses connection-pool reuse under
    concurrent load (issue #1907 retry policy).
    """

    def test_both_clients_are_singletons_within_registry(self):
        """Calling ``registry.get("anthropic")`` twice returns the same
        client instance — pooling / connection reuse must not be broken
        by per-request lookup.
        """
        from upstream_registry import get_upstream_registry  # type: ignore[import-not-found]

        registry = get_upstream_registry()
        client_a, _ = registry.get("anthropic")
        client_b, _ = registry.get("anthropic")
        assert client_a is client_b

    def test_litellm_client_is_singleton(self):
        """LiteLLM client is similarly cached so the SSE retry loop's
        pool semantics match the Anthropic path."""
        from upstream_registry import get_upstream_registry  # type: ignore[import-not-found]

        registry = get_upstream_registry()
        client_a, _ = registry.get("litellm")
        client_b, _ = registry.get("litellm")
        assert client_a is client_b

    def test_anthropic_and_litellm_are_distinct_clients(self):
        """The two upstreams must NOT share an httpx.Client — different
        base_url / credential semantics.
        """
        from upstream_registry import get_upstream_registry  # type: ignore[import-not-found]

        registry = get_upstream_registry()
        anthropic_client, _ = registry.get("anthropic")
        litellm_client, _ = registry.get("litellm")
        assert anthropic_client is not litellm_client
