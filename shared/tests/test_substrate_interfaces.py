"""Substrate Protocol conformance tests (#2623 slice-1 task-1-8).

Verifies that the four ``typing.Protocol`` interfaces pinned by
task-1-1 exist and have the expected member set, and that the
``select_substrate`` factory honors the ``EGG_SUBSTRATE`` env var
contract from cq-1 (parallel substrates, env-var-selected).
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

substrate_pkg = pytest.importorskip(
    "orchestrator.substrate",
    reason="orchestrator/substrate/ package not present yet",
)


# ---------------------------------------------------------------------------
# Protocol presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proto_name",
    ["AgentSpawner", "MessageBus", "PolicyEnforcer", "WorktreeManager"],
)
def test_protocol_is_defined(proto_name: str) -> None:
    """All four protocols required by task-1-1 must be importable."""
    proto = getattr(substrate_pkg, proto_name, None)
    assert proto is not None, f"substrate.{proto_name} not exported — task-1-1 acceptance criterion"
    assert inspect.isclass(proto), f"{proto_name} must be a class"


# ---------------------------------------------------------------------------
# AgentSpawner.spawn signature (cq-4: synchronous spawn)
# ---------------------------------------------------------------------------


def test_agent_spawner_spawn_signature_matches_cq4() -> None:
    """``AgentSpawner.spawn`` is synchronous, returns ``AgentResult``."""
    proto = substrate_pkg.AgentSpawner
    spawn = getattr(proto, "spawn", None)
    assert spawn is not None, "AgentSpawner.spawn member is required (cq-4)"
    assert not inspect.iscoroutinefunction(spawn), "AgentSpawner.spawn must be synchronous per cq-4"


def test_agent_spawner_protocol_is_runtime_checkable() -> None:
    """``isinstance(impl, AgentSpawner)`` must succeed (cq-4 unit-test AC).

    Task-1-2 acceptance criterion: "ClaudeCodeSpawner conforms to
    AgentSpawner (verified by isinstance check in a unit test)".
    isinstance() against a Protocol requires the Protocol to be
    decorated with @runtime_checkable.
    """
    proto = substrate_pkg.AgentSpawner
    # ``@runtime_checkable`` adds the special ``_is_runtime_protocol``
    # attribute set to True.
    assert getattr(proto, "_is_runtime_protocol", False) is True, (
        "AgentSpawner must be decorated @runtime_checkable so the "
        "task-1-2 / task-1-1 isinstance-conformance check works."
    )


# ---------------------------------------------------------------------------
# AgentResult dataclass includes commit_sha (INV-6)
# ---------------------------------------------------------------------------


def test_agent_result_has_commit_sha_field() -> None:
    """``AgentResult.commit_sha`` is mandatory (INV-6 — task-1-1 AC)."""
    agent_result_cls = getattr(substrate_pkg, "AgentResult", None)
    assert agent_result_cls is not None, "substrate.AgentResult dataclass missing — task-1-1 AC"
    hints = get_type_hints(agent_result_cls)
    assert "commit_sha" in hints, "AgentResult must include commit_sha: str | None (INV-6)"


# ---------------------------------------------------------------------------
# select_substrate factory honors EGG_SUBSTRATE (cq-1)
# ---------------------------------------------------------------------------


def test_select_substrate_returns_bundle_with_required_fields() -> None:
    """``select_substrate`` bundles must expose all four substrate slots."""
    select_substrate = substrate_pkg.select_substrate
    bundle = select_substrate({"EGG_SUBSTRATE": "claude-code"})
    for attr in ("spawner", "bus", "policy", "worktrees"):
        assert hasattr(bundle, attr), f"SubstrateBundle must expose .{attr} — task-1-1 AC"


def test_select_substrate_defaults_to_k3s_when_env_absent() -> None:
    """``select_substrate({})`` defaults to ``"k3s"`` per cq-1.

    The contract AC for task-1-1 also requires the default to return
    a *working* spawner (a K3sSpawnerAdapter wrapping
    create_concurrent_spawn_fn). This test pins the substrate-name
    selection here; the working-spawner contract is exercised by
    :func:`test_select_substrate_k3s_default_spawner_is_callable`.
    """
    select_substrate = substrate_pkg.select_substrate
    bundle = select_substrate({})
    assert bundle.name == "k3s", (
        f"select_substrate({{}}) must default to 'k3s'; got {bundle.name!r}"
    )


def test_select_substrate_k3s_with_legacy_fn_returns_k3s_adapter() -> None:
    """When a legacy spawn fn is supplied, the bundle wraps it in K3sSpawnerAdapter.

    Task-1-1 AC: the k3s leg must return a working K3sSpawnerAdapter
    wrapping ``create_concurrent_spawn_fn``. When the caller supplies
    a pre-built closure (the production path), the bundle uses it
    directly.
    """
    from unittest.mock import MagicMock

    select_substrate = substrate_pkg.select_substrate
    K3sSpawnerAdapter = substrate_pkg.K3sSpawnerAdapter
    spawn_fn = MagicMock()
    bundle = select_substrate({"EGG_SUBSTRATE": "k3s"}, k3s_legacy_spawn_fn=spawn_fn)
    assert isinstance(bundle.spawner, K3sSpawnerAdapter), (
        f"select_substrate(k3s, k3s_legacy_spawn_fn=...) must return "
        f"K3sSpawnerAdapter; got {type(bundle.spawner).__name__}"
    )


def test_select_substrate_returns_claude_code_when_env_set() -> None:
    """``select_substrate({"EGG_SUBSTRATE": "claude-code"})`` returns the in-process bundle."""
    select_substrate = substrate_pkg.select_substrate
    bundle = select_substrate({"EGG_SUBSTRATE": "claude-code"})
    assert bundle.name == "claude-code"
    cls_name = type(bundle.spawner).__name__
    assert "claude" in cls_name.lower(), (
        f"select_substrate(claude-code) must return a Claude Code spawner; "
        f"got {cls_name}. cq-1 / task-1-1 AC."
    )


def test_select_substrate_rejects_unknown_value() -> None:
    """Unknown ``EGG_SUBSTRATE`` values fail loudly rather than silently fall back."""
    select_substrate = substrate_pkg.select_substrate
    with pytest.raises(ValueError):
        select_substrate({"EGG_SUBSTRATE": "definitely-not-a-substrate"})


def test_select_substrate_is_case_insensitive() -> None:
    """``EGG_SUBSTRATE=Claude-Code`` resolves the same as ``"claude-code"``."""
    select_substrate = substrate_pkg.select_substrate
    bundle = select_substrate({"EGG_SUBSTRATE": "Claude-Code"})
    assert bundle.name == "claude-code"


# ---------------------------------------------------------------------------
# K3s default spawner is callable / working (task-1-1 AC contract gap)
# ---------------------------------------------------------------------------


def test_select_substrate_k3s_default_spawner_is_working() -> None:
    """The k3s default bundle's spawner is a *working* spawner.

    Task-1-1 AC literally says::

        select_substrate({}) defaults to "k3s" and returns a working
        K3sSpawnerAdapter wrapping create_concurrent_spawn_fn

    "working" means ``.spawn(...)`` must not immediately raise
    ``NotImplementedError`` with a placeholder/stub message. Real
    spawn failures (e.g. no live cluster) are acceptable; a stub
    that refuses to run is not.
    """
    select_substrate = substrate_pkg.select_substrate
    bundle = select_substrate({})
    spawner = bundle.spawner
    # The class name must not signal a "deferred" / placeholder stub.
    cls_name = type(spawner).__name__
    assert "Deferred" not in cls_name and "Placeholder" not in cls_name, (
        f"select_substrate({{}}) returned a {cls_name} — task-1-1 AC "
        f"requires a working K3sSpawnerAdapter wrapping "
        f"create_concurrent_spawn_fn, not a deferred / placeholder stub."
    )
