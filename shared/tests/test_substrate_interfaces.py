"""Substrate Protocol conformance tests (#2623 slice-1 task-1-8).

Verifies that the four ``typing.Protocol`` interfaces pinned by
task-1-1 exist and have the expected member set, and that the
``select_substrate`` factory honors the ``EGG_SUBSTRATE`` env var
contract from cq-1 (parallel substrates, env-var-selected).

Per the plan, these tests live in ``shared/tests/`` rather than
``orchestrator/tests/`` because they exercise the Protocol surface,
not any substrate-specific behavior.
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

substrate_pkg = pytest.importorskip(
    "substrate",
    reason="orchestrator/substrate/ package not present yet (task-1-1 pending)",
)


# ---------------------------------------------------------------------------
# Protocol presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proto_name",
    ["AgentSpawner", "MessageBus", "PolicyEnforcer", "WorktreeManager"],
)
def test_protocol_is_defined(proto_name: str) -> None:
    """All four protocols required by task-1-1 must be importable from substrate."""
    proto = getattr(substrate_pkg, proto_name, None)
    assert proto is not None, (
        f"substrate.{proto_name} not exported — task-1-1 acceptance criterion"
    )
    # typing.Protocol bases — runtime-checkable not required by the plan,
    # but the class must look like a Protocol (has __mro__).
    assert inspect.isclass(proto), f"{proto_name} must be a class"


# ---------------------------------------------------------------------------
# AgentSpawner.spawn signature (cq-4)
# ---------------------------------------------------------------------------


def test_agent_spawner_spawn_signature_matches_cq4() -> None:
    """``AgentSpawner.spawn`` is synchronous, returns ``AgentResult``."""
    proto = getattr(substrate_pkg, "AgentSpawner")
    spawn = getattr(proto, "spawn", None)
    assert spawn is not None, "AgentSpawner.spawn member is required (cq-4)"
    # spawn must be a regular function (synchronous) — not async.
    assert not inspect.iscoroutinefunction(spawn), (
        "AgentSpawner.spawn must be synchronous per cq-4"
    )


# ---------------------------------------------------------------------------
# AgentResult dataclass includes commit_sha (INV-6)
# ---------------------------------------------------------------------------


def test_agent_result_has_commit_sha_field() -> None:
    """``AgentResult.commit_sha`` is mandatory (INV-6 — task-1-1 AC)."""
    agent_result_cls = getattr(substrate_pkg, "AgentResult", None)
    assert agent_result_cls is not None, (
        "substrate.AgentResult dataclass missing — task-1-1 AC"
    )
    hints = get_type_hints(agent_result_cls)
    assert "commit_sha" in hints, (
        "AgentResult must include commit_sha: str | None (INV-6)"
    )


# ---------------------------------------------------------------------------
# select_substrate factory honors EGG_SUBSTRATE (cq-1)
# ---------------------------------------------------------------------------


def test_select_substrate_defaults_to_k3s_when_env_absent() -> None:
    """``select_substrate({})`` returns the k3s bundle by default."""
    select_substrate = getattr(substrate_pkg, "select_substrate")
    bundle = select_substrate({})
    # The k3s leg wraps create_concurrent_spawn_fn — pinned by task-1-1
    # acceptance criterion. The exact class name is K3sSpawnerAdapter
    # per the plan, but we accept any spawner whose class name contains
    # "k3s" or "K3s" (case-insensitive) to be lenient on naming.
    spawner = bundle.spawner
    assert spawner is not None
    cls_name = type(spawner).__name__
    assert "k3s" in cls_name.lower(), (
        f"select_substrate({{}}) default must return a k3s spawner; got "
        f"{cls_name}. cq-1 / task-1-1 AC."
    )


def test_select_substrate_returns_claude_code_when_env_set() -> None:
    """``select_substrate({"EGG_SUBSTRATE": "claude-code"})`` returns the in-process bundle."""
    select_substrate = getattr(substrate_pkg, "select_substrate")
    bundle = select_substrate({"EGG_SUBSTRATE": "claude-code"})
    spawner = bundle.spawner
    cls_name = type(spawner).__name__
    assert "claude" in cls_name.lower(), (
        f"select_substrate(claude-code) must return a Claude Code spawner; "
        f"got {cls_name}. cq-1 / task-1-1 AC."
    )


def test_select_substrate_rejects_unknown_value() -> None:
    """Unknown ``EGG_SUBSTRATE`` values fail loudly rather than silently fall back."""
    select_substrate = getattr(substrate_pkg, "select_substrate")
    with pytest.raises((ValueError, KeyError, RuntimeError)):
        select_substrate({"EGG_SUBSTRATE": "definitely-not-a-substrate"})
