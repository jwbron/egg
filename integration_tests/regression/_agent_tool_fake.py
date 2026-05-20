"""Test-only nested-Agent-tool dispatch fake (#2717 TASK-1-9).

Simulates Claude Code's ``Agent`` tool by spawning a subprocess with a
controlled ``EGG_AGENT_ROLE`` env var per dispatch. Each fake-subagent
exposes a ``pre_tool_use_callback`` that invokes
``orchestrator.substrate.claude_code.hook_entry.decide(...)`` with the
tool input — the same code path Claude Code's PreToolUse hook would
follow in real Agent-tool dispatch. The fake exists so TASK-1-5 (R2
spike: ``test_pretooluse_hook_nested.py``) can drive a deterministic
nested-dispatch scenario without standing up a real Claude Code
session — there isn't one available in the in-sandbox-agent
trust context, and the production
``orchestrator/substrate/claude_code/spawner.py`` is a harness re-host
that bypasses the PreToolUse hook by design (cq-3: the harness uses
its own ``ToolRegistry.set_permission_callback(...)``; the hook is NOT
in its tool-call loop).

**This is TEST INFRASTRUCTURE ONLY.** It is NOT a production spawner
and is NOT registered in ``orchestrator.substrate.select_substrate``;
the import guard at the bottom of the module rejects any production
caller. Production dispatch stays on ``ClaudeCodeSpawner`` per cq-3
("decide empirically post-implement") — the empirical-answer half of
the R2 question (does Claude Code itself propagate ``EGG_AGENT_ROLE``
correctly under real nested dispatch?) becomes load-bearing only when
cq-3 flips to Agent-tool dispatch in a future issue.

What R2 validates with this fake
--------------------------------

R2 (issue #2623) asks: when a parent agent dispatches a child agent
via the Agent tool, does the PreToolUse hook resolve the *child's*
role correctly, so a write that violates the child's allow-list is
denied even when the parent's role would allow it?

This fake's ``dispatch(parent_role, child_role, write_target)`` helper
answers the **hook-logic half** of R2: it spawns a child subprocess
with ``EGG_AGENT_ROLE=<child_role>`` (mirroring what Claude Code's
Agent tool would set), feeds a Write tool input to
``hook_entry.decide(...)`` inside that subprocess, and returns the
verdict. The hook reads the env-set role; the fake validates that
the resulting verdict matches the *child's* allow-list (not the
parent's). The remaining empirical half — "does Claude Code itself
set ``EGG_AGENT_ROLE`` correctly under nested dispatch?" — is
verifiable only from inside a real Claude Code session, which the
in-sandbox-agent test context cannot provide. The R2 spike's test
docstring documents this empirical-vs-test-fake limitation.

State-serialization contract
----------------------------

For risk_analyst R17 mitigation: the same ``pending_hitl`` envelope
schema invented by the flattened-bridge driver
(``plugins/egg-sdlc/skills/egg-sdlc/bin/run_pipeline.py`` —
``PENDING_HITL_SCHEMA_VERSION`` constant) flows through this fake. A
parent fake-subagent can write a decision envelope through the same
contract-file path the production driver uses, and the slice-3
daemon variant
(``orchestrator/substrate/claude_code/hitl_daemon.py`` — TASK-3-2)
inherits the same envelope schema. The fake re-exports
``PENDING_HITL_SCHEMA_VERSION`` so tests can pin the version they
assert against without re-deriving the constant.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Import guard: refuse to be imported by anything that isn't a test module.
# Tests under ``integration_tests/regression/`` (the only intended caller),
# and the colocated TASK-1-5 test that re-exports the fake via direct
# attribute access. We use ``__name__`` because the import system has not
# yet set ``__package__`` reliably for test discovery shapes; both
# ``integration_tests.regression._agent_tool_fake`` and the bare
# ``_agent_tool_fake`` shapes are accepted.
# ---------------------------------------------------------------------------
_ALLOWED_MODULE_PREFIXES: tuple[str, ...] = (
    "integration_tests",  # collected via the test-tree path
    "_agent_tool_fake",  # bare path when conftest's sys.path injection lands
    "__main__",  # smoke-run via ``python3 -m``
)
if not any(__name__.startswith(prefix) for prefix in _ALLOWED_MODULE_PREFIXES):
    raise ImportError(
        "_agent_tool_fake.py is test infrastructure only — it must not be "
        "imported by production code. Import path "
        f"{__name__!r} did not start with any of "
        f"{_ALLOWED_MODULE_PREFIXES!r}. See the module docstring for why "
        "this guard exists (cq-3: production stays on the harness re-host)."
    )


# Re-export so test bodies can pin the version they expect without
# re-deriving the constant.
try:
    # When the egg-sdlc skill ships alongside the source tree, the
    # driver's module is importable via a path-walk.
    _SKILL_BIN_DIR = (
        Path(__file__).resolve().parent.parent.parent
        / "plugins"
        / "egg-sdlc"
        / "skills"
        / "egg-sdlc"
        / "bin"
    )
    sys.path.insert(0, str(_SKILL_BIN_DIR))
    try:
        from run_pipeline import (  # type: ignore[import-not-found,import-untyped,unused-ignore]
            PENDING_HITL_SCHEMA_VERSION,
        )
    finally:
        try:
            sys.path.remove(str(_SKILL_BIN_DIR))
        except ValueError:  # pragma: no cover — defensive
            pass
except ImportError:  # pragma: no cover — defensive
    # Hard-code the version so tests can still import even if the
    # skill bin isn't present (uncommon — but the fake should not
    # implode if the driver is in flight).
    PENDING_HITL_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Public dataclass surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of a single nested fake-Agent-tool dispatch.

    Attributes:
        parent_role: The ``EGG_AGENT_ROLE`` of the parent fake-subagent.
        child_role: The ``EGG_AGENT_ROLE`` of the child fake-subagent.
        write_target: The repo-relative path the child attempted to write.
        decision: The dict returned by ``hook_entry.decide(...)``. Empty
            dict means "allow"; ``{"decision": "block", "reason": "..."}``
            means deny.
        denied: Convenience boolean — True iff ``decision["decision"]``
            is ``"block"``.
        deny_reason: The block reason text (empty when ``denied`` is
            False).
        child_pid: PID of the child subprocess (for debugging).
        child_exit_code: Exit code of the child subprocess (0 when the
            child ran ``decide`` to completion; nonzero when the
            subprocess hit an internal error).
        stderr: Captured stderr from the child subprocess.
    """

    parent_role: str
    child_role: str
    write_target: str
    decision: dict[str, Any]
    denied: bool
    deny_reason: str
    child_pid: int
    child_exit_code: int
    stderr: str


# ---------------------------------------------------------------------------
# Child-subprocess entry — invoked via ``python3 -m`` from the parent.
# ---------------------------------------------------------------------------


def _child_main(argv: list[str]) -> int:
    """Child-subprocess entry point.

    Reads ``stdin_blob`` (the simulated Claude Code PreToolUse stdin)
    from ``argv[1]`` (path to a JSON file written by the parent),
    invokes ``hook_entry.decide(...)``, prints the resulting verdict to
    stdout as JSON, and exits 0.

    The child process is intentionally minimal: it imports
    ``orchestrator.substrate.claude_code.hook_entry`` and calls
    ``decide(stdin_blob)`` directly — no Claude Code session, no Agent
    tool, no harness. The relevant input to ``decide`` is the env
    (``EGG_AGENT_ROLE`` set by the parent on the subprocess) and the
    JSON blob (the simulated tool input). What we are validating in
    the test is the hook *logic*: given accurate env propagation, does
    the hook deny the right writes?
    """
    if len(argv) < 2:
        print("_agent_tool_fake child: missing stdin-blob path argv[1]", file=sys.stderr)
        return 2
    blob_path = Path(argv[1])
    try:
        stdin_blob = json.loads(blob_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"_agent_tool_fake child: cannot read stdin blob: {exc}", file=sys.stderr)
        return 2

    # Lazy import inside the child so an environment without the
    # orchestrator package surfaces the ImportError to the parent
    # (where it is converted into a structured DispatchResult).
    try:
        from orchestrator.substrate.claude_code.hook_entry import decide
    except ImportError as exc:
        print(
            f"_agent_tool_fake child: cannot import hook_entry.decide: {exc}",
            file=sys.stderr,
        )
        return 3

    verdict = decide(stdin_blob)
    json.dump(verdict if isinstance(verdict, dict) else {}, sys.stdout)
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# Parent-side public helpers
# ---------------------------------------------------------------------------


def pre_tool_use_callback(
    role: str,
    tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    extra_env: Mapping[str, str] | None = None,
    python_executable: str | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Invoke ``hook_entry.decide(...)`` as if it were the PreToolUse
    callback for a child fake-subagent running with ``EGG_AGENT_ROLE=role``.

    The fake spawns a fresh ``python3 -m integration_tests.regression
    ._agent_tool_fake <stdin_blob.json>`` subprocess with the env
    isolated to a controlled set — mirroring what Claude Code's Agent
    tool would set when invoking a child subagent. Returns the
    verdict dict (empty == allow; ``{"decision": "block", "reason":
    "..."}`` == deny).

    Args:
        role: ``EGG_AGENT_ROLE`` to set on the child subprocess.
        tool_name: Name Claude Code would pass on stdin (``"Write"``,
            ``"Edit"``, ``"Bash"``, etc.).
        tool_input: Tool input dict Claude Code would pass on stdin.
        extra_env: Optional additional env vars to merge into the
            child subprocess (useful for ``EGG_REPO_ROOT`` etc.).
        python_executable: Optional override for the Python
            interpreter; defaults to ``sys.executable``.
        timeout: Wall-clock cap on the child subprocess (seconds).
    """
    py = python_executable or sys.executable
    blob = {"tool_name": tool_name, "tool_input": dict(tool_input)}

    # Write the stdin blob to a temp file. We avoid pipes-to-stdin
    # here so the child can be invoked with ``python3 -m`` cleanly —
    # the ``-m`` invocation expects argv-driven input. The path is
    # unlinked at the end of the function.
    import tempfile

    blob_fd, blob_path = tempfile.mkstemp(prefix="agent_tool_fake_", suffix=".json")
    try:
        with os.fdopen(blob_fd, "w", encoding="utf-8") as fp:
            json.dump(blob, fp)

        env = {**os.environ, "EGG_AGENT_ROLE": role}
        if extra_env:
            env.update(extra_env)

        # Repo-root resolution — the test typically passes a tmp_path
        # via ``extra_env={"EGG_REPO_ROOT": ...}``; if it doesn't, the
        # hook treats writes as "outside any repo root" and the
        # symlink-resolution branch is skipped, which is fine for the
        # role-routing-under-nested-dispatch question R2 asks.

        # Locate this module's path so the child can ``-m`` it. We
        # prefer the package-qualified shape so the child's sys.path
        # mirrors the test runner's.
        module_name = (
            "integration_tests.regression._agent_tool_fake"
            if __name__.startswith("integration_tests")
            else "_agent_tool_fake"
        )

        # Ensure the repo root is on PYTHONPATH so the child can
        # resolve both the orchestrator package and this fake module.
        repo_root = Path(__file__).resolve().parent.parent.parent
        pythonpath = os.pathsep.join(
            p for p in (str(repo_root), str(repo_root / "shared"), env.get("PYTHONPATH")) if p
        )
        env["PYTHONPATH"] = pythonpath

        completed = subprocess.run(
            [py, "-m", module_name, blob_path],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(repo_root),
        )
        if completed.returncode != 0:
            # Surface a structured deny that names the failure so the
            # test sees an actionable diagnostic rather than a silent
            # allow.
            return {
                "decision": "block",
                "reason": (
                    f"_agent_tool_fake child exited {completed.returncode}; "
                    f"stderr={completed.stderr.strip()!r}"
                ),
                "_fake_child_exit_code": completed.returncode,
                "_fake_child_stderr": completed.stderr,
            }
        try:
            verdict = json.loads(completed.stdout) if completed.stdout.strip() else {}
        except json.JSONDecodeError:
            return {
                "decision": "block",
                "reason": (
                    f"_agent_tool_fake child produced non-JSON stdout: {completed.stdout!r}"
                ),
                "_fake_child_exit_code": completed.returncode,
                "_fake_child_stderr": completed.stderr,
            }
        if not isinstance(verdict, dict):
            verdict = {}
        return verdict
    finally:
        try:
            Path(blob_path).unlink(missing_ok=True)
        except OSError:  # pragma: no cover — defensive
            pass


def dispatch(
    parent_role: str,
    child_role: str,
    write_target: str,
    *,
    tool_name: str = "Write",
    extra_env: Mapping[str, str] | None = None,
    repo_root: str | os.PathLike[str] | None = None,
) -> DispatchResult:
    """Simulate a parent fake-subagent dispatching a child fake-subagent
    that attempts to write ``write_target``.

    The parent fake-subagent is *implicit* — only ``parent_role`` is
    recorded; the fake does not spawn a parent subprocess because the
    R2 question is about whether the **child's** role is correctly
    resolved by the PreToolUse hook. A real Agent-tool dispatch would
    set ``EGG_AGENT_ROLE`` to the child's role on the child subagent's
    process; the fake mirrors that exactly.

    Args:
        parent_role: The parent fake-subagent's role. Recorded for
            audit / observability; the parent subprocess is not
            spawned because role-routing happens at the child boundary.
        child_role: The child fake-subagent's role — set as
            ``EGG_AGENT_ROLE`` on the child subprocess so
            ``hook_entry.decide(...)`` resolves it.
        write_target: The repo-relative path the child attempts to
            write. The fake constructs a Write tool input with
            ``file_path=write_target``.
        tool_name: Tool name to put on stdin (``"Write"`` by default;
            tests can override to ``"Edit"`` / ``"Bash"`` to exercise
            other branches of the hook's path-extractor).
        extra_env: Optional extra env vars for the child subprocess.
        repo_root: Optional ``EGG_REPO_ROOT`` to set on the child. Most
            R2 tests pass a tmp_path so the hook's repo-relative
            resolver behaves deterministically.

    Returns:
        A ``DispatchResult`` with the structured outcome.
    """
    # Construct the tool input shape Claude Code would pass on
    # PreToolUse stdin for a Write call.
    tool_input: dict[str, Any] = {"file_path": write_target}
    if tool_name == "Bash":
        # Bash uses ``command`` instead of ``file_path``; tests
        # exercising the Bash branch can pass a command shape directly.
        tool_input = {"command": write_target}

    env_extra: dict[str, str] = dict(extra_env or {})
    if repo_root is not None:
        env_extra.setdefault("EGG_REPO_ROOT", str(repo_root))
        env_extra.setdefault("EGG_WORKTREE_ROOT", str(repo_root))

    verdict = pre_tool_use_callback(
        child_role,
        tool_name,
        tool_input,
        extra_env=env_extra,
    )
    denied = bool(verdict.get("decision") == "block")
    deny_reason = str(verdict.get("reason") or "") if denied else ""
    return DispatchResult(
        parent_role=parent_role,
        child_role=child_role,
        write_target=write_target,
        decision=verdict,
        denied=denied,
        deny_reason=deny_reason,
        child_pid=int(verdict.get("_fake_child_pid", 0) or 0),
        child_exit_code=int(verdict.get("_fake_child_exit_code", 0) or 0),
        stderr=str(verdict.get("_fake_child_stderr", "") or ""),
    )


# ---------------------------------------------------------------------------
# pending_hitl envelope helpers — slice-3 daemon shares this contract.
# ---------------------------------------------------------------------------


def build_pending_hitl_envelope(
    pipeline_id: str,
    *,
    decision: dict[str, Any] | None = None,
    answer: Any = None,
    status: str = "pending",
) -> dict[str, Any]:
    """Construct a ``pending_hitl`` envelope dict matching the schema
    invented in TASK-1-1.

    Helper for tests that want to round-trip an envelope through the
    fake without re-deriving the field set. The slice-3 daemon
    (TASK-3-2) is expected to accept envelopes built by this helper.
    """
    from datetime import UTC, datetime

    return {
        "version": PENDING_HITL_SCHEMA_VERSION,
        "pipeline_id": pipeline_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "decision": decision,
        "answer": answer,
        "status": status,
        "result": None,
        "error": None,
        "answer_log": [],
    }


# ---------------------------------------------------------------------------
# Module entry point (for ``python3 -m integration_tests.regression
# ._agent_tool_fake <blob.json>`` invocations by the parent helper).
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv))
