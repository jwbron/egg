"""Symbol-level import smoke test for claude-agent-sdk.

If the SDK version pinned in ``sandbox/pyproject.toml`` drops or renames
either ``create_sdk_mcp_server`` or ``tool``, CI will fail at
test-collection time with a message pointing at the SDK release notes.

This is TASK-6-1 from the plan: pin the SDK to ``>=0.1.65,<0.2`` and
guard the surface used by ``sandbox/egg_agent_tools``.

The test is skipped outside the sandbox (where the SDK is not installed)
so it behaves gracefully in CI — the real enforcement is the
pyproject.toml bound + the Dockerfile ARG default.
"""

from __future__ import annotations

import pytest


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
        "range like '>=0.1.65,<0.2' so the SDK cannot auto-upgrade past "
        "tested surface"
    )
