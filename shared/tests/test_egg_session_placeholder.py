"""Unit tests for :mod:`egg_session_placeholder` codec."""

from __future__ import annotations

from egg_session_placeholder import (
    PLACEHOLDER_PREFIX,
    from_placeholder,
    to_placeholder,
)


def test_to_placeholder_starts_with_sk_ant_oat01() -> None:
    # Claude Code accepts ``sk-ant-oat01-*`` tokens by prefix; anything
    # else fails its local format check at startup.
    placeholder = to_placeholder("abc123")
    assert placeholder.startswith("sk-ant-oat01-")


def test_roundtrip() -> None:
    token = "xK7-aB_dEfGhIjKlMnOpQrStUvWxYz0123456789AbC"
    assert from_placeholder(to_placeholder(token)) == token


def test_from_placeholder_strips_bearer_prefix() -> None:
    token = "xK7-aB_dEfGhIjKlMnOpQrStUvWxYz0123456789AbC"
    placeholder = to_placeholder(token)
    assert from_placeholder(f"Bearer {placeholder}") == token


def test_from_placeholder_returns_none_for_real_oauth_token() -> None:
    # A real Claude OAuth token (or any header value that doesn't match
    # the placeholder envelope) must not be misread as a session token.
    assert from_placeholder("sk-ant-oat01-actualrealoauthtokencontents") is None


def test_from_placeholder_returns_none_for_empty_and_none() -> None:
    assert from_placeholder("") is None
    assert from_placeholder(None) is None


def test_from_placeholder_returns_none_when_suffix_empty() -> None:
    # The prefix alone is not a valid session marker.
    assert from_placeholder(PLACEHOLDER_PREFIX) is None
