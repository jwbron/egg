"""Tests for ``PreToolUseHookPolicy`` (#2623 slice-1 task-1-4, task-1-8).

Acceptance criteria covered:

* ``PreToolUseHookPolicy.check_write(role, path)`` denies out-of-role
  writes — match behavior with
  ``gateway/phase_filter.py:1061 check_agent_restrictions``.
* The accompanying ``hook_entry.decide`` function returns
  ``{"decision": "block", "reason": ...}`` for blocked tool calls,
  ``{}`` for allowed.
* The hook entry script reads JSON on stdin and prints JSON on stdout
  per the Claude Code PreToolUse hook protocol.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

substrate_pkg = pytest.importorskip(
    "orchestrator.substrate",
    reason="orchestrator/substrate/ package not present yet",
)
policy_mod = pytest.importorskip(
    "orchestrator.substrate.claude_code.policy",
    reason="orchestrator/substrate/claude_code/policy.py not present yet",
)
hook_entry_mod = pytest.importorskip(
    "orchestrator.substrate.claude_code.hook_entry",
    reason="orchestrator/substrate/claude_code/hook_entry.py not present yet",
)


# ---------------------------------------------------------------------------
# check_write — in-process enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,blocked_path,note",
    [
        # Tester cannot write source files.
        ("tester", "orchestrator/concurrent_executor.py", "tester→source"),
        # Coder cannot write docs.
        ("coder", "docs/architecture/claude-code-substrate.md", "coder→docs"),
        # Documenter cannot write source.
        ("documenter", "orchestrator/concurrent_executor.py", "documenter→source"),
    ],
)
def test_check_write_denies_out_of_role_writes(role: str, blocked_path: str, note: str) -> None:
    """``check_write`` returns ``(False, reason)`` for blocked role+path combos."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    allowed, reason = policy.check_write(role, blocked_path)
    assert allowed is False, f"{note}: expected denial; got allowed=True"
    assert reason, f"{note}: denial must carry a non-empty reason"


@pytest.mark.parametrize(
    "role,allowed_path",
    [
        ("tester", "shared/tests/test_substrate_interfaces.py"),
        ("coder", "orchestrator/substrate/spawner.py"),
        ("documenter", "docs/architecture/claude-code-substrate.md"),
    ],
)
def test_check_write_allows_in_role_writes(role: str, allowed_path: str) -> None:
    """``check_write`` returns ``(True, None)`` for in-role writes."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    allowed, reason = policy.check_write(role, allowed_path)
    assert allowed is True, f"{role}→{allowed_path} expected allowed; got reason={reason!r}"
    assert reason is None


def test_check_write_no_role_allows_everything() -> None:
    """When ``role`` is empty the policy fail-opens."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    allowed, reason = policy.check_write("", "orchestrator/anything.py")
    assert allowed is True
    assert reason is None


# ---------------------------------------------------------------------------
# decide() — hook-shape contract (block vs allow)
# ---------------------------------------------------------------------------


def test_decide_blocks_out_of_role_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """``hook_entry.decide`` returns ``{"decision": "block", ...}`` on denial."""
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    monkeypatch.setenv("EGG_REPO_ROOT", "/home/egg/repos/egg")
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/home/egg/repos/egg/orchestrator/concurrent_executor.py",
            "content": "hi",
        },
    }
    result = hook_entry_mod.decide(payload)
    assert result.get("decision") == "block", (
        f"hook_entry.decide must block tester→source write; got {result!r}"
    )
    assert "reason" in result and result["reason"]


def test_decide_allows_in_role_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """``hook_entry.decide`` returns ``{}`` for in-role writes (allow)."""
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    monkeypatch.setenv("EGG_REPO_ROOT", "/home/egg/repos/egg")
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": ("/home/egg/repos/egg/shared/tests/test_substrate_interfaces.py"),
            "content": "import pytest",
        },
    }
    result = hook_entry_mod.decide(payload)
    assert result == {} or "decision" not in result, (
        f"hook_entry.decide must allow tester→test-file; got {result!r}"
    )


def test_decide_fail_open_when_role_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``EGG_AGENT_ROLE``, the hook fail-opens (no enforcement)."""
    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/home/egg/repos/egg/orchestrator/concurrent_executor.py",
            "content": "...",
        },
    }
    result = hook_entry_mod.decide(payload)
    assert result == {} or "decision" not in result, (
        "Without EGG_AGENT_ROLE the hook must fail-open"
    )


def test_decide_ignores_read_only_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only tools (``Read``, ``Bash``) get no decision (allow)."""
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": "/anything"},
    }
    result = hook_entry_mod.decide(payload)
    assert result == {} or "decision" not in result


# ---------------------------------------------------------------------------
# Hook entry script: stdin JSON → stdout JSON contract
# ---------------------------------------------------------------------------


def test_hook_entry_script_blocks_out_of_role_write(
    tmp_path: Path,
) -> None:
    """The hook entry script emits ``decision=block`` on stdout for blocked calls."""
    hook_entry = Path("/home/egg/repos/egg/orchestrator/substrate/claude_code/hook_entry.py")
    if not hook_entry.exists():
        pytest.skip(f"{hook_entry} not present")

    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "/home/egg/repos/egg/orchestrator/concurrent_executor.py",
            "content": "hi",
        },
    }
    env = dict(os.environ)
    env["EGG_AGENT_ROLE"] = "tester"
    env["EGG_REPO_ROOT"] = "/home/egg/repos/egg"
    # The script may need to import egg_restrictions; thread the
    # project root onto sys.path via PYTHONPATH.
    env["PYTHONPATH"] = (
        "/home/egg/repos/egg/shared"
        + os.pathsep
        + "/home/egg/repos/egg/orchestrator"
        + os.pathsep
        + "/home/egg/repos/egg"
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    proc = subprocess.run(
        [sys.executable, str(hook_entry)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )
    # The Claude Code hook protocol carries the decision in stdout
    # JSON, not the exit code. Exit code 0 is the normal "I ran"
    # signal; the block decision is in stdout.
    assert proc.returncode == 0, (
        f"hook script must exit 0 on normal completion; got rc={proc.returncode}, "
        f"stderr={proc.stderr!r}"
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"hook script stdout must be valid JSON; got {proc.stdout!r} ({exc})")
    assert out.get("decision") == "block", (
        f"hook script must emit decision=block for tester→source write; got stdout={proc.stdout!r}"
    )


def test_hook_entry_script_allows_in_role_write(tmp_path: Path) -> None:
    """The hook entry script emits ``{}`` for in-role writes (allow)."""
    hook_entry = Path("/home/egg/repos/egg/orchestrator/substrate/claude_code/hook_entry.py")
    if not hook_entry.exists():
        pytest.skip(f"{hook_entry} not present")

    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": ("/home/egg/repos/egg/shared/tests/test_substrate_interfaces.py"),
            "content": "...",
        },
    }
    env = dict(os.environ)
    env["EGG_AGENT_ROLE"] = "tester"
    env["EGG_REPO_ROOT"] = "/home/egg/repos/egg"
    env["PYTHONPATH"] = (
        "/home/egg/repos/egg/shared"
        + os.pathsep
        + "/home/egg/repos/egg/orchestrator"
        + os.pathsep
        + "/home/egg/repos/egg"
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    proc = subprocess.run(
        [sys.executable, str(hook_entry)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert out == {} or "decision" not in out, (
        f"hook script must allow tester→test-file; got stdout={proc.stdout!r}"
    )


# ---------------------------------------------------------------------------
# install() — settings.json templating
# ---------------------------------------------------------------------------


def test_install_writes_settings_json(tmp_path: Path) -> None:
    """``install`` writes ``.claude/settings.json`` containing the hook."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    out = policy.install(tmp_path)
    assert out.exists()
    settings = json.loads(out.read_text())
    assert "hooks" in settings, "settings.json must include a 'hooks' block"


def test_install_is_idempotent(tmp_path: Path) -> None:
    """Re-running ``install`` does not duplicate the egg hook."""
    PreToolUseHookPolicy = policy_mod.PreToolUseHookPolicy
    policy = PreToolUseHookPolicy()
    out1 = policy.install(tmp_path)
    first = json.loads(out1.read_text())
    policy.install(tmp_path)  # second run
    second = json.loads(out1.read_text())
    assert first == second, "Repeated install() calls must produce byte-identical settings.json"
