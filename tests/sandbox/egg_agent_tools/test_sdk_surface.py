"""Symbol-level import smoke test for claude-agent-sdk.

If the SDK version pinned in ``sandbox/pyproject.toml`` drops or renames
any of the symbols egg imports, CI will fail at test-collection time
with a message pointing at the SDK release notes.

This is TASK-6-1 from the plan: pin the SDK to ``>=0.2.97,<0.3`` and
guard the surface used by ``sandbox/egg_agent_tools`` and the
sandbox-side wrapper ``shared/egg_agent/client.py``.

The test is skipped outside the sandbox (where the SDK is not installed)
so it behaves gracefully in CI — the real enforcement is the
pyproject.toml bound + the Dockerfile ARG default.
"""

from __future__ import annotations

import pytest

# Symbols imported by ``shared/egg_agent/client.py`` — a 0.3 bump that
# renames or drops any of these must update this list AND the importer
# in lockstep, or agent spawn breaks with ``ImportError`` at runtime.
# Sourced from ``shared/egg_agent/client.py:219-235`` and the
# ``CLIJSONDecodeError`` reference in the same block (#2804 marker).
_EGG_AGENT_CLIENT_SYMBOLS = (
    "AssistantMessage",
    "ClaudeAgentOptions",
    "ClaudeSDKError",
    "CLIJSONDecodeError",
    "CLINotFoundError",
    "HookMatcher",
    "PermissionResultAllow",
    "PermissionResultDeny",
    "ProcessError",
    "ResultMessage",
    "SystemMessage",
    "TextBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "UserMessage",
    "query",
)


def test_sdk_exposes_create_sdk_mcp_server() -> None:
    try:
        import claude_agent_sdk
    except ImportError:
        pytest.skip("claude_agent_sdk not installed in this environment")

    assert hasattr(claude_agent_sdk, "create_sdk_mcp_server"), (
        "claude-agent-sdk removed create_sdk_mcp_server — check release notes "
        "and update the pin in sandbox/pyproject.toml"
    )


def test_sdk_exposes_tool_decorator() -> None:
    try:
        import claude_agent_sdk
    except ImportError:
        pytest.skip("claude_agent_sdk not installed in this environment")

    assert hasattr(claude_agent_sdk, "tool"), (
        "claude-agent-sdk removed the @tool decorator — check release notes "
        "and update the pin in sandbox/pyproject.toml"
    )


@pytest.mark.parametrize("symbol", _EGG_AGENT_CLIENT_SYMBOLS)
def test_sdk_exposes_egg_agent_client_symbols(symbol: str) -> None:
    """Guard every symbol ``shared/egg_agent/client.py`` imports from the SDK.

    A 0.3 release that drops any of these would crash-loop agent spawn
    with ``ImportError`` at the top of ``run_agent_async`` — surface it
    at CI time instead. Update both this list and the importer when
    bumping the pin.
    """
    try:
        import claude_agent_sdk
    except ImportError:
        pytest.skip("claude_agent_sdk not installed in this environment")

    assert hasattr(claude_agent_sdk, symbol), (
        f"claude-agent-sdk no longer exposes {symbol!r} — "
        "check release notes, update shared/egg_agent/client.py, "
        "and update the pin in sandbox/pyproject.toml"
    )


def test_sandbox_pyproject_pins_sdk() -> None:
    """Defend the pin in sandbox/pyproject.toml — a missing bound lets
    the SDK auto-upgrade past tested surface.  If this test fails,
    restore the ``claude-agent-sdk>=X,<Y`` form."""
    import re
    from pathlib import Path

    pyproj = Path(__file__).resolve().parents[3] / "sandbox" / "pyproject.toml"
    text = pyproj.read_text()
    # Match either a quoted string entry or a table entry.
    assert re.search(r"claude-agent-sdk[^\"'\n]*>=[^,]*,[^\"'\n]*<[^\"'\n]+", text), (
        "sandbox/pyproject.toml must pin claude-agent-sdk with a bounded "
        "range like '>=0.2.97,<0.3' so the SDK cannot auto-upgrade past "
        "tested surface"
    )
