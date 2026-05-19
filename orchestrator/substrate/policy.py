"""Policy-enforcement protocol for the substrate-swap spike (#2623).

See ``docs/architecture/claude-code-substrate.md`` for the broader
substrate model. This module defines the ``PolicyEnforcer`` protocol
every substrate implementation must satisfy.

HITL decision cq-6 pins the policy seam to **PreToolUse hooks** for
the Claude Code substrate: a ``.claude/settings.json``-registered hook
script intercepts Write/Edit/Bash calls before they execute and
denies anything that would land outside the caller's role's allow-
list. The hook uses
``shared/egg_restrictions/patterns.py:768 build_agent_patterns`` —
the **same** symbol the gateway path uses in
``gateway/phase_filter.py:1061 check_agent_restrictions`` — so the two
substrates share a single source of truth for file boundaries.

INTERFACE STABILITY: v0.x unstable.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PolicyEnforcer(Protocol):
    """Protocol every substrate policy enforcer must satisfy.

    The protocol surfaces two operations:

    1. ``check_write`` — synchronous "may this role write this path?"
       used by hook scripts and any in-process validator.
    2. ``install`` — set up the substrate-side enforcement primitive
       (e.g., write a ``.claude/settings.json`` template for the
       Claude Code substrate; no-op for the gateway-backed k3s
       substrate, where enforcement is already wired up).
    """

    def check_write(self, role: str, path: str) -> tuple[bool, str | None]:
        """Return ``(allowed, message)`` for a proposed write.

        Args:
            role: Agent role making the write (e.g. ``"coder"``).
            path: Repo-relative path the role wants to write.

        Returns:
            Tuple of ``(allowed, denial_message)``. When
            ``allowed=True``, the denial message is ``None``. When
            ``allowed=False``, the message describes why and matches
            the format used by
            ``gateway/phase_filter.py:1061 check_agent_restrictions``.
        """
        ...

    def install(self, target_dir: str) -> None:
        """Install the substrate's enforcement primitive at
        ``target_dir``.

        For the Claude Code substrate, this writes
        ``.claude/settings.json`` referencing the PreToolUse hook
        entry. For the k3s substrate, this is a no-op (the gateway
        sidecar enforces structurally).
        """
        ...
