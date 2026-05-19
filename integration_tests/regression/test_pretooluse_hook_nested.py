"""PreToolUse hook nested-dispatch test (#2717 slice-1 task-1-5, cq-5 early-spike).

The R2 question — *"does the egg PreToolUse hook resolve agent role
correctly under nested Agent-tool dispatch?"* — is the gating empirical
finding for the slice-5 R15 migration (flipping production dispatch
from the harness re-host model to Claude Code's Agent tool). Slice 1
gives a partial-but-load-bearing answer: **the hook logic is correct
given accurate ``EGG_AGENT_ROLE`` propagation**; the remaining half
(does Claude Code itself propagate ``EGG_AGENT_ROLE`` into nested
subagents in real production dispatch?) is verifiable only against a
real Claude Code session and is deferred to slice-5 / a future issue
when ``ClaudeCodeSpawner`` actually exercises Agent-tool dispatch.

Why this is a test-fake test, not an empirical Claude-Code test
---------------------------------------------------------------
``shared/egg_harness/client.py:60-150`` is the harness re-host model
(per cq-3): subagents run as fresh ``ClaudeCodeSpawner`` invocations,
not as Agent-tool dispatches inside a parent session. ``grep -rn
"PreToolUseHookPolicy|hook_entry" shared/egg_harness/`` returns zero
hits — the harness wires its own ``ToolRegistry.set_permission_callback``
and never reaches ``hook_entry.decide``. So under the production
substrate today the PreToolUse hook is **not** even invoked for the
"nested" leg.

To answer R2 deterministically the test uses the test-only fake from
task-1-9 (``integration_tests/regression/_agent_tool_fake.py``). The
fake simulates the nested dispatch by spawning a subprocess with a
controlled ``EGG_AGENT_ROLE`` and routing the simulated tool input
through ``hook_entry.decide(...)``. This **pins the hook logic** —
when slice-5 flips dispatch to Agent-tool and ``EGG_AGENT_ROLE``
propagation becomes the production reality, the same logic ships
unchanged.

Acceptance criteria covered (per contract task-1-5):

* ``hook_entry.decide(...)`` returns ``{"decision": "block", ...}`` for
  a child write to ``orchestrator/foo.py`` when the child's role is
  ``tester`` (out of role for source files) — even though the parent
  role is ``architect``.
* The verdict is written to
  ``.egg-state/<pipeline_id>/r2-verdict.json`` as
  ``{"r2_verdict": "pass"}`` (or ``"fail"`` with a reason) so slice-5's
  contingent R15 migration task can read it.
* The hook entry script reads JSON on stdin and prints JSON on stdout
  per the Claude Code PreToolUse hook protocol — exercised
  end-to-end via the fake.

Test runs in <60s per the AC.

Fake API shape
--------------
TASK-1-9's ``dispatch(...)`` returns a ``DispatchResult`` dataclass
(``parent_role``, ``child_role``, ``write_target``, ``decision`` —
the raw hook-verdict dict — and convenience fields ``denied`` /
``deny_reason``). Test assertions use the structured dataclass
attributes so a re-shape of the underlying verdict dict (e.g. adding
extra metadata keys) does not break the test contract.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


#: Path the coder commits the nested-dispatch fake to per task-1-9.
_FAKE_MODULE_PATH = Path(__file__).parent / "_agent_tool_fake.py"


# ---------------------------------------------------------------------------
# Fixture — load the coder-owned fake helper from task-1-9
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake() -> object:
    """Import the task-1-9 fake module.

    Skip if the fake is not yet present (coder dependency) so the test
    file does not break collection while task-1-9 is in flight.
    """
    if not _FAKE_MODULE_PATH.exists():
        pytest.skip(
            f"{_FAKE_MODULE_PATH.name} not present — task-1-9 (coder) "
            f"has not landed yet. This is the upstream dependency for "
            f"the R2 nested-dispatch test."
        )
    # Force a fresh import each time so a regression in the fake's
    # module-level state doesn't leak across tests.
    sys.modules.pop("integration_tests.regression._agent_tool_fake", None)
    return importlib.import_module("integration_tests.regression._agent_tool_fake")


@pytest.fixture()
def isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox the ``.egg-state/<pipeline_id>/`` tree for the r2-verdict write.

    The test writes ``.egg-state/<pipeline_id>/r2-verdict.json`` under
    a tmp tree so a re-run does not silently overwrite a real
    pipeline's verdict.
    """
    state = tmp_path / ".egg-state"
    state.mkdir()
    monkeypatch.chdir(tmp_path)
    return state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_r2_verdict(state_dir: Path, pipeline_id: str, payload: dict) -> Path:
    """Write the R2 verdict under ``.egg-state/<pipeline_id>/r2-verdict.json``.

    Returns the path so callers can pin it in the assertions.
    """
    out_dir = state_dir / pipeline_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "r2-verdict.json"
    out_path.write_text(json.dumps(payload, indent=2))
    return out_path


# ---------------------------------------------------------------------------
# Test — R2 verdict (gating finding for slice-5 R15)
# ---------------------------------------------------------------------------


def test_pretooluse_hook_denies_nested_child_write(fake: object, isolated_state_dir: Path) -> None:
    """The hook denies a child write outside the child's role.

    Setup:
      * Parent fake-subagent role: ``architect`` — recorded only for
        observability (the fake does not spawn a parent subprocess
        because role-routing happens at the child boundary in real
        Agent-tool dispatch).
      * Child fake-subagent role: ``tester`` — NOT allowed to write
        ``orchestrator/foo.py`` (testers are scoped to ``tests/``,
        ``**/conftest.py``, etc. per the role boundaries declared by
        ``shared/egg_restrictions/patterns.py``).

    When the child subprocess invokes ``hook_entry.decide(...)`` with
    ``Write {file_path: "orchestrator/foo.py"}`` and
    ``EGG_AGENT_ROLE=tester``, the hook must return
    ``{"decision": "block", "reason": ...}`` — proving the hook does
    NOT silently default to a parent-side role when the child env
    carries the correct role.

    Pass criterion: ``DispatchResult.denied is True`` and the deny
    reason references the tester role.
    """
    pipeline_id = "pipeline-r2-nested"

    dispatch = fake.dispatch
    assert callable(dispatch), (
        f"task-1-9 contract: ``_agent_tool_fake.dispatch`` must be "
        f"callable; module exposes {dir(fake)!r}"
    )

    result = dispatch(
        parent_role="architect",
        child_role="tester",
        write_target="orchestrator/foo.py",
    )

    # Derive the verdict from the dispatch outcome — slice-5's
    # contingent R15 migration task reads ``r2-verdict.json`` to
    # decide whether to proceed, so the file must reflect the
    # empirical answer, not an optimistic constant. Write the
    # verdict *before* the assertions so a regression that fails
    # one of the structured checks below still produces an
    # accurate ``{"r2_verdict": "fail", "reason": ...}`` record
    # for the downstream consumer (reviewer_code finding #5
    # non-blocking).
    verdict = getattr(result, "decision", None) or {}
    reason = str(verdict.get("reason") or "")
    if (
        getattr(result, "denied", None) is True
        and isinstance(verdict, dict)
        and verdict.get("decision") == "block"
        and "tester" in reason.lower()
    ):
        verdict_payload: dict[str, object] = {"r2_verdict": "pass"}
    else:
        verdict_payload = {
            "r2_verdict": "fail",
            "reason": (
                f"DispatchResult denied={getattr(result, 'denied', None)!r}; "
                f"raw_decision={verdict!r}; reason={reason!r}"
            ),
        }
    verdict_path = _write_r2_verdict(isolated_state_dir, pipeline_id, verdict_payload)
    assert verdict_path.exists()
    written = json.loads(verdict_path.read_text())
    # Always-asserted shape — the field is mandatory either way.
    assert "r2_verdict" in written, (
        f"r2-verdict.json must encode an 'r2_verdict' field per AC; got {written!r}"
    )

    # AC: hook returns ``{"decision": "block", "reason": ...}`` for
    # the child's denied write. The fake wraps this in a
    # ``DispatchResult``; pin both the structured ``denied`` bool and
    # the raw verdict dict so a refactor of either surface is caught.
    assert getattr(result, "denied", None) is True, (
        f"R2 nested-dispatch verdict must be ``denied`` for "
        f"tester→orchestrator/foo.py; got {result!r}. This indicates "
        f"the hook resolved the role from the parent rather than the "
        f"child — slice-5's R15 migration cannot ship until this is fixed."
    )
    assert isinstance(verdict, dict) and verdict, (
        f"DispatchResult.decision must be a non-empty dict (the raw hook "
        f"verdict); got {type(verdict).__name__} ({verdict!r})"
    )
    assert verdict.get("decision") == "block", (
        f"raw hook verdict must carry ``decision='block'`` on a denied dispatch; got {verdict!r}"
    )
    assert verdict.get("reason"), (
        f"``block`` verdict must carry a non-empty ``reason`` — "
        f"reviewer_security finding pattern. Got {verdict!r}"
    )
    # Reason should name the tester role — operator reading the
    # Claude Code UI denial needs the resolved role to act on it.
    assert "tester" in reason.lower(), (
        f"denial reason must name the resolved (child) role so the "
        f"operator can act on it; got {reason!r}"
    )
    # And the verdict file we wrote reflects the pass path.
    assert written.get("r2_verdict") == "pass", (
        f"on a passing run the verdict file must record 'pass'; got {written!r}"
    )


def test_pretooluse_hook_allows_in_role_child_write(fake: object, isolated_state_dir: Path) -> None:
    """Negative-control: in-role child write is NOT spuriously denied.

    Without this, a "deny everything" regression would silently pass
    ``test_pretooluse_hook_denies_nested_child_write`` while breaking
    every legitimate write. Pin the allow path explicitly.
    """
    dispatch = fake.dispatch

    result = dispatch(
        parent_role="architect",
        child_role="tester",
        write_target="integration_tests/regression/test_example.py",
    )

    assert getattr(result, "denied", None) is False, (
        f"in-role child write (tester→integration_tests/regression/) "
        f"must NOT be denied; got denied=True (verdict={getattr(result, 'decision', None)!r}). "
        f"A regression here would deny every legitimate tester write "
        f"under nested dispatch."
    )


def test_pretooluse_hook_blocks_parent_role_with_child_write_target(
    fake: object, isolated_state_dir: Path
) -> None:
    """Cross-role probe: parent ``coder`` + child ``tester`` writing source must deny.

    Adversarial probe: even when the *parent* role would also be
    denied for this write (coder cannot write to ``shared/tests/``),
    the hook must surface the CHILD's denial reason — proving the
    nested-dispatch resolution actually uses the child env, not a
    parent-side fallback that happens to also block.
    """
    dispatch = fake.dispatch

    # Parent and child have different roles; the write target is
    # outside BOTH roles' allow-lists. The hook must still resolve
    # the child's role (tester) and emit a tester-scoped denial.
    result = dispatch(
        parent_role="coder",
        child_role="tester",
        write_target="orchestrator/concurrent_executor.py",
    )

    assert getattr(result, "denied", None) is True, (
        f"parent=coder, child=tester writing orchestrator/* must be "
        f"denied by the hook; got {result!r}"
    )
    reason = str(getattr(result, "deny_reason", "") or "")
    # The denial reason must name the CHILD role (tester) — if it
    # named the parent (coder), that would be the role-resolution
    # bug R2 is asking about.
    assert "tester" in reason.lower(), (
        f"R2 bug signature: nested-dispatch denial named the parent "
        f"role rather than the child. reason={reason!r}. The hook is "
        f"resolving role from the wrong process env; slice-5 R15 "
        f"migration is blocked until this is fixed."
    )


# ---------------------------------------------------------------------------
# Adversarial probing: env-propagation invariants the fake must hold
# ---------------------------------------------------------------------------


def test_dispatch_returns_structured_result(fake: object) -> None:
    """``dispatch(...)`` returns a ``DispatchResult`` with the verdict dict.

    Acceptance: ``dispatch(...)`` returns the hook verdict (the same
    JSON-decoded dict ``hook_entry.decide`` writes to stdout). The
    coder's task-1-9 implementation wraps the verdict in a
    ``DispatchResult`` dataclass whose ``.decision`` field IS the dict —
    pin both surfaces so a refactor of either is caught.
    """
    dispatch = fake.dispatch
    result = dispatch(
        parent_role="architect",
        child_role="tester",
        write_target="orchestrator/foo.py",
    )
    # Structural shape.
    assert hasattr(result, "parent_role") and result.parent_role == "architect"
    assert hasattr(result, "child_role") and result.child_role == "tester"
    assert hasattr(result, "write_target") and result.write_target == "orchestrator/foo.py"
    assert hasattr(result, "decision") and isinstance(result.decision, dict), (
        f"DispatchResult.decision must be the raw hook verdict dict; "
        f"got {type(getattr(result, 'decision', None)).__name__}"
    )
    assert hasattr(result, "denied") and isinstance(result.denied, bool)


def test_dispatch_does_not_leak_egg_agent_role_into_parent_env(
    fake: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fake must not mutate the caller's ``EGG_AGENT_ROLE``.

    Adversarial: a fake that calls ``os.environ['EGG_AGENT_ROLE']=...``
    instead of passing env to the subprocess would leak the simulated
    child role into the test process — every subsequent test that
    relies on ``EGG_AGENT_ROLE`` would see the leaked value. The fake
    must isolate the env via subprocess ``env=`` (or equivalent).
    """
    dispatch = fake.dispatch

    monkeypatch.delenv("EGG_AGENT_ROLE", raising=False)
    _ = dispatch(
        parent_role="architect",
        child_role="tester",
        write_target="orchestrator/foo.py",
    )

    assert os.environ.get("EGG_AGENT_ROLE", "") == "", (
        f"fake.dispatch leaked EGG_AGENT_ROLE into the parent env "
        f"(value={os.environ.get('EGG_AGENT_ROLE')!r}). This would "
        f"corrupt every subsequent test in the same process."
    )
