"""Tests for ``PreToolUseHookPolicy`` (#2623 slice-1 task-1-4, task-1-8).

Acceptance criteria covered:

* ``PreToolUseHookPolicy`` conforms to the ``PolicyEnforcer`` Protocol.
* Denies out-of-role writes — i.e. when invoked with a role and a
  blocked path, returns a denial decision that matches the behavior
  of ``gateway/phase_filter.py:1061 check_agent_restrictions``.
* The accompanying ``hook_entry.py`` script returns a non-zero exit
  code when the proposed tool call writes to a path outside the
  caller's allowed pattern set.

The behavioral oracle is
``shared/egg_restrictions/patterns.py`` — the single source of truth
both gateway and sandbox sides consult — and
``gateway/agent_restrictions.py::partition_files_by_role``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

substrate_pkg = pytest.importorskip(
    "substrate",
    reason="orchestrator/substrate/ package not present yet (task-1-1 pending)",
)
claude_code_pkg = pytest.importorskip(
    "substrate.claude_code",
    reason=(
        "orchestrator/substrate/claude_code/ package not present yet "
        "(task-1-4 pending)"
    ),
)
policy_mod = pytest.importorskip(
    "substrate.claude_code.policy",
    reason="substrate.claude_code.policy module not present yet (task-1-4)",
)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_pretooluse_hook_policy_class_exported() -> None:
    """``PreToolUseHookPolicy`` is importable and has the policy surface."""
    policy_cls = getattr(policy_mod, "PreToolUseHookPolicy", None)
    assert policy_cls is not None, (
        "substrate.claude_code.policy.PreToolUseHookPolicy missing — "
        "task-1-4 AC"
    )


# ---------------------------------------------------------------------------
# Behavioral: denies out-of-role writes per shared/egg_restrictions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,blocked_path,reason",
    [
        # Tester can write tests but not source code under orchestrator/.
        ("tester", "orchestrator/concurrent_executor.py", "tester→source"),
        # Coder can write source but not docs or tests under tests/.
        ("coder", "docs/architecture/claude-code-substrate.md", "coder→docs"),
        # Documenter can write docs but not source.
        ("documenter", "orchestrator/concurrent_executor.py", "documenter→source"),
    ],
)
def test_policy_denies_out_of_role_writes(
    role: str,
    blocked_path: str,
    reason: str,
) -> None:
    """Out-of-role writes are denied by the policy enforcer."""
    policy_cls = getattr(policy_mod, "PreToolUseHookPolicy")
    policy = policy_cls()
    # TODO(tester): once the coder pins the policy.check() signature,
    # tighten this assertion. The plan implies a shape like
    #   decision = policy.check(role=role, write_path=blocked_path)
    #   assert not decision.allowed
    # but the exact dataclass / return contract is set by task-1-4.
    pytest.skip(
        f"PreToolUseHookPolicy.check() signature pending — fill in once "
        f"task-1-4 lands (covers {reason})"
    )


# ---------------------------------------------------------------------------
# Behavioral: allow in-role writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,allowed_path",
    [
        ("tester", "shared/tests/test_substrate_interfaces.py"),
        ("coder", "orchestrator/substrate/spawner.py"),
        ("documenter", "docs/architecture/claude-code-substrate.md"),
    ],
)
def test_policy_allows_in_role_writes(role: str, allowed_path: str) -> None:
    """In-role writes pass the policy enforcer."""
    policy_cls = getattr(policy_mod, "PreToolUseHookPolicy")
    policy = policy_cls()
    pytest.skip(
        "PreToolUseHookPolicy.check() signature pending — fill in once "
        "task-1-4 lands"
    )


# ---------------------------------------------------------------------------
# Hook entry script: exits non-zero on blocked path
# ---------------------------------------------------------------------------


def test_hook_entry_script_blocks_out_of_role_write(
    tmp_path: Path,
) -> None:
    """The hook entry script returns non-zero when the call is blocked.

    The Claude Code PreToolUse hook protocol expects the script to read
    JSON on stdin describing the proposed tool call and exit non-zero
    (with a stderr message) to refuse it. We invoke the script with a
    payload that should be blocked under tester role and assert the
    non-zero exit.
    """
    hook_entry = Path(
        "/home/egg/repos/egg/orchestrator/substrate/claude_code/hook_entry.py"
    )
    if not hook_entry.exists():
        pytest.skip(
            f"{hook_entry} not present yet — task-1-4 pending"
        )

    payload = {
        "tool": "Write",
        "params": {
            "file_path": "orchestrator/concurrent_executor.py",
            "content": "hi",
        },
        "agent_role": "tester",
    }
    proc = subprocess.run(
        [sys.executable, str(hook_entry)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    # TODO(tester): tighten once the coder pins the hook protocol —
    # the Claude Code hook surface uses specific exit codes (e.g. 2)
    # vs. plain non-zero. For now, any non-zero is a denial.
    assert proc.returncode != 0, (
        f"hook_entry must deny tester→source write; got rc={proc.returncode}, "
        f"stderr={proc.stderr!r}"
    )
