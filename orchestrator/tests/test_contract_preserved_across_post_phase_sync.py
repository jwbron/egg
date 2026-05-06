"""Regression test for #2488: agent contract writes survive post-phase sync.

Agents register HITL questions via ``register_open_question`` /
``request_feedback``.  Those calls go through the gateway → orchestrator's
``/api/v1/contracts/<id>/mutate`` endpoint, which writes the live
contract file in the *shared* pipeline worktree at
``.egg-state/contracts/<pipeline_id>.json``.  The writes are uncommitted
on disk.

At phase end, the orchestrator runs ``_sync_worktree_with_remote`` to
incorporate agent-pushed drafts/code from origin.  Two paths through that
helper run ``git reset --hard origin/<branch>`` and so will discard
uncommitted working-tree changes:

* ``local_ahead == 0 and remote_ahead > 0`` (local-behind) — falls
  through to the step-4 reset.
* ``local_ahead > 0 and remote_ahead == 0`` plus ``prior_phase_succeeded``
  with a failed push — also falls through to the step-4 reset.

Without intervention, the agent's contract decisions are wiped on those
paths before the bridge (``_queue_and_await_contract_decisions``) has a
chance to surface them.

The fix: commit ``.egg-state/`` ahead of the sync so the agent's contract
writes become part of the local branch tip and survive any subsequent
reset/rebase.  The pre-sync commit also turns a "local-behind" state
into a "diverged" state, which the helper resolves via rebase rather
than reset — preserving the change in the canonical way.

These tests use real git plumbing (no subprocess mocks) so the
commit/sync interaction is verified against ground truth.  Test 1
demonstrates the bug at the bare ``git reset`` level.  Tests 2 and 3
exercise ``_sync_worktree_with_remote`` directly with a gateway stub
that performs real ``git fetch`` / ``git push`` against a local bare
remote, so the assertion holds against the actual production sync code
path — not a hand-rolled approximation of it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from gateway_client import PushResult
from routes.pipelines import (
    _commit_statefiles_to_worktree,
    _sync_worktree_with_remote,
)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _setup_repo_with_remote(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Create a bare remote, a working clone, and a feature branch.

    Seeds an initial contract file committed on ``branch`` so the
    contract path is *tracked* — matching production where the
    pipeline's first checkpoint commits the bootstrap contract before
    the agent ever runs.  ``git reset --hard`` reverts modifications to
    tracked files (which is the bug); untracked files would not be
    touched by reset, so testing the failure mode requires a tracked
    starting point.

    Returns ``(worktree, remote, branch, identifier)``.
    """
    identifier = "issue-42"
    remote = tmp_path / "remote.git"
    _git("init", "--bare", str(remote), cwd=tmp_path)

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git("init", cwd=worktree)
    # Configure local git identity so commits made via subprocess.run
    # without GIT_AUTHOR_* env vars (e.g. _commit_statefiles_to_worktree)
    # succeed on CI runners that have no global git config.
    _git("config", "user.email", "test@example.com", cwd=worktree)
    _git("config", "user.name", "test", cwd=worktree)
    _git("remote", "add", "origin", str(remote), cwd=worktree)

    # Initial commit on main, then branch off to a feature branch.
    (worktree / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=worktree)
    _git("commit", "-m", "seed", cwd=worktree)
    _git("branch", "-M", "main", cwd=worktree)
    _git("push", "-u", "origin", "main", cwd=worktree)

    branch = f"egg/{identifier}/work"
    _git("checkout", "-b", branch, cwd=worktree)

    # Seed the contract on the branch so it is tracked in HEAD.
    contracts_dir = worktree / ".egg-state" / "contracts"
    contracts_dir.mkdir(parents=True)
    (contracts_dir / f"{identifier}.json").write_text(
        '{"schema_version":"1.0","pipeline_id":"' + identifier + '",'
        '"current_phase":"refine","decisions":[]}\n'
    )
    _git("add", f".egg-state/contracts/{identifier}.json", cwd=worktree)
    _git("commit", "-m", "bootstrap contract", cwd=worktree)
    _git("push", "-u", "origin", branch, cwd=worktree)

    return worktree, remote, branch, identifier


def _push_agent_draft_to_origin(remote: Path, branch: str, tmp_path: Path) -> None:
    """Push an agent-style commit (draft file) to ``origin/{branch}``.

    Simulates a refiner/reviewer agent pushing draft work from its own
    per-agent worktree during the phase. Done by cloning the bare remote
    to a side-channel worktree, committing, and pushing.
    """
    side = tmp_path / "agent_side"
    _git("clone", "-b", branch, str(remote), str(side), cwd=tmp_path)
    drafts_dir = side / ".egg-state" / "drafts"
    drafts_dir.mkdir(parents=True)
    (drafts_dir / "42-analysis.md").write_text("agent analysis\n")
    _git("add", ".egg-state/drafts/42-analysis.md", cwd=side)
    _git("commit", "-m", "agent: add analysis draft", cwd=side)
    _git("push", "origin", branch, cwd=side)


def _agent_writes_decision_to_contract(worktree: Path, identifier: str) -> Path:
    """Modify the on-disk contract to add an agent-registered decision.

    Mirrors what the orchestrator's ``/api/v1/contracts/<id>/mutate``
    endpoint does on each ``register_open_question`` call — overwrite
    the JSON in place and leave the modification uncommitted.
    """
    path = worktree / ".egg-state" / "contracts" / f"{identifier}.json"
    path.write_text(
        '{"schema_version":"1.0","pipeline_id":"' + identifier + '",'
        '"current_phase":"refine","decisions":[{"id":"decision-1",'
        '"question":"Which database?","type":"hitl","phase":"refine",'
        '"options":[{"id":"opt-1","label":"Postgres","description":null}],'
        '"resolved":false,"resolution":null,"resolved_by":null,'
        '"resolved_at":null,"debounce_until":null}]}\n'
    )
    return path


def _make_gateway_stub_with_real_git(worktree: Path) -> MagicMock:
    """Build a ``spawner`` mock whose gateway calls run real git commands.

    ``_sync_worktree_with_remote`` only reaches into the spawner via
    ``spawner.gateway.fetch_worktree_branch`` and
    ``spawner.gateway.push_worktree_branch``.  Stubbing those two surfaces
    with real ``git fetch`` / ``git push`` against the local bare remote
    is enough to exercise the production helper end-to-end without
    bringing up the gateway sidecar.

    The stub mirrors the gateway's contract:

    * ``fetch_worktree_branch`` returns ``True`` on success / ``False``
      on failure.
    * ``push_worktree_branch`` returns a ``PushResult`` (truthy on
      success).  The gateway uses the ``HEAD:refs/heads/{branch}``
      refspec form, so the stub does the same.
    """
    spawner = MagicMock()

    def real_fetch(pipeline_id: str, repo_path: str, mode: str) -> bool:  # noqa: ARG001
        result = subprocess.run(
            ["git", "-C", repo_path, "fetch", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return result.returncode == 0

    def real_push(
        pipeline_id: str,  # noqa: ARG001
        repo_path: str,
        branch: str,
        mode: str,  # noqa: ARG001
        base_branch: str | None = None,  # noqa: ARG001
    ) -> PushResult:
        result = subprocess.run(
            ["git", "-C", repo_path, "push", "origin", f"HEAD:refs/heads/{branch}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode == 0:
            return PushResult(ok=True)
        return PushResult(ok=False, category="push_rejected", detail=result.stderr)

    spawner.gateway.fetch_worktree_branch.side_effect = real_fetch
    spawner.gateway.push_worktree_branch.side_effect = real_push
    return spawner


def test_uncommitted_contract_decisions_lost_when_sync_resets_without_pre_commit(
    tmp_path: Path,
) -> None:
    """Demonstrates the bug: a bare ``git reset --hard origin/<branch>``
    after the agent has mutated the tracked contract reverts the
    modifications.

    This is the failure mode the fix prevents: the bridge runs after
    sync, so if sync clobbers ``contract.decisions``, the bridge has
    nothing to surface and the operator never sees the agent's
    questions (#2488).
    """
    worktree, remote, branch, identifier = _setup_repo_with_remote(tmp_path)
    _push_agent_draft_to_origin(remote, branch, tmp_path)
    _git("fetch", "origin", cwd=worktree)

    contract_path = _agent_writes_decision_to_contract(worktree, identifier)
    assert "decision-1" in contract_path.read_text()

    # Simulate _sync_worktree_with_remote's reset path with no pre-commit.
    _git("reset", "--hard", f"origin/{branch}", cwd=worktree)

    assert "decision-1" not in contract_path.read_text(), (
        "Without a pre-sync commit, git reset --hard reverts the agent's "
        "uncommitted contract modification — the symptom of #2488."
    )


def test_pre_sync_commit_preserves_agent_decisions_through_real_sync_helper(
    tmp_path: Path,
) -> None:
    """End-to-end against the real ``_sync_worktree_with_remote`` helper.

    The fix: ``_commit_statefiles_to_worktree`` ahead of the sync
    captures the agent's contract modification.  The pre-sync commit
    moves the worktree from ``local_ahead == 0 and remote_ahead > 0``
    (which would have hit the step-4 reset) to
    ``local_ahead > 0 and remote_ahead > 0`` (which routes through the
    rebase path).  Calling ``_sync_worktree_with_remote`` with a gateway
    stub that performs real ``git fetch`` / ``git push`` exercises the
    actual production code path — proving that the helper's rebase
    branch reconciles the contract commit with the agent's pushed draft.

    A future regression in ``_sync_worktree_with_remote`` (e.g. adding
    ``git checkout -- .egg-state/`` before reset) would fail this test,
    closing the gap left by the lower-level git-only tests.
    """
    worktree, remote, branch, identifier = _setup_repo_with_remote(tmp_path)
    _push_agent_draft_to_origin(remote, branch, tmp_path)

    contract_path = _agent_writes_decision_to_contract(worktree, identifier)
    expected_body = contract_path.read_text()

    # Pre-sync commit — the new step added by the #2488 fix.
    _commit_statefiles_to_worktree(
        worktree,
        "Persist agent statefile writes before refine sync",
        pipeline_identifier=42,
        pipeline_id=identifier,
    )

    # Run the production sync helper directly.  The stub gateway
    # performs real git fetch/push against the bare remote.
    spawner = _make_gateway_stub_with_real_git(worktree)
    _sync_worktree_with_remote(
        spawner,
        identifier,
        worktree,
        prior_phase_succeeded=True,
        gateway_mode="public",
        base_branch="main",
        pipeline_branch=branch,
    )

    # Contract decision survived the sync.
    assert contract_path.read_text() == expected_body
    assert "decision-1" in contract_path.read_text()
    # The agent's draft commit from origin was also brought in.
    assert (worktree / ".egg-state/drafts/42-analysis.md").exists()


def test_real_sync_helper_loses_decisions_without_pre_sync_commit(
    tmp_path: Path,
) -> None:
    """Negative case against the real ``_sync_worktree_with_remote`` helper.

    Skipping the pre-sync commit must leave the local-behind path
    falling through to ``git reset --hard origin/<branch>``, which
    discards the uncommitted contract modification.  Pairs with the
    positive test above so the helper itself is exercised on both
    sides of the fix — if a future change to the helper accidentally
    started preserving uncommitted ``.egg-state/`` writes, this test
    would fail and prompt re-evaluation of whether the pre-sync commit
    is still necessary.
    """
    worktree, remote, branch, identifier = _setup_repo_with_remote(tmp_path)
    _push_agent_draft_to_origin(remote, branch, tmp_path)

    contract_path = _agent_writes_decision_to_contract(worktree, identifier)
    assert "decision-1" in contract_path.read_text()

    # NOTE: deliberately no _commit_statefiles_to_worktree() call here.
    spawner = _make_gateway_stub_with_real_git(worktree)
    _sync_worktree_with_remote(
        spawner,
        identifier,
        worktree,
        prior_phase_succeeded=True,
        gateway_mode="public",
        base_branch="main",
        pipeline_branch=branch,
    )

    assert "decision-1" not in contract_path.read_text(), (
        "Without the pre-sync commit, _sync_worktree_with_remote's "
        "local-behind path resets to origin and discards the agent's "
        "uncommitted contract decision — the bug fixed in #2488."
    )
