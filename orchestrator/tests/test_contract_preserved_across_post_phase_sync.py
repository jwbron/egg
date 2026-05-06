"""Regression test for #2488: agent contract writes survive post-phase sync.

Agents register HITL questions via ``register_open_question`` /
``request_feedback``.  Those calls go through the gateway → orchestrator's
``/api/v1/contracts/<id>/mutate`` endpoint, which writes the live
contract file in the *shared* pipeline worktree at
``.egg-state/contracts/<pipeline_id>.json``.  The writes are uncommitted
on disk.

At phase end, the orchestrator runs ``_sync_worktree_with_remote`` to
incorporate agent-pushed drafts/code from origin.  When the local branch
is behind origin (the typical case — agents pushed during the phase),
that helper falls through to ``git reset --hard origin/<branch>``, which
discards uncommitted working-tree changes.  Without intervention, the
agent's contract decisions are wiped before the bridge
(``_queue_and_await_contract_decisions``) has a chance to surface them.

The fix: commit ``.egg-state/`` ahead of the sync so the agent's contract
writes become part of the local branch tip and survive any subsequent
reset/rebase.  This test exercises the real git plumbing end-to-end —
no subprocess mocks — so that the commit/sync interaction is verified
against ground truth.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing modules that depend on it
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

from routes.pipelines import _commit_statefiles_to_worktree  # noqa: E402


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


def test_pre_sync_commit_preserves_agent_decisions_through_rebase(
    tmp_path: Path,
) -> None:
    """The fix: ``_commit_statefiles_to_worktree`` ahead of the sync
    captures the agent's contract modification so the rebase path
    cannot discard it.  The commit makes it part of the local branch
    tip, which the sync's rebase reconciles with origin.
    """
    worktree, remote, branch, identifier = _setup_repo_with_remote(tmp_path)
    _push_agent_draft_to_origin(remote, branch, tmp_path)
    _git("fetch", "origin", cwd=worktree)

    contract_path = _agent_writes_decision_to_contract(worktree, identifier)
    expected_body = contract_path.read_text()

    # Pre-sync commit — the new step added by the #2488 fix.
    _commit_statefiles_to_worktree(
        worktree,
        "Persist agent statefile writes before refine sync",
        pipeline_identifier=42,
        pipeline_id=identifier,
    )

    # Now rebase local onto origin/<branch> (the divergence path the real
    # ``_sync_worktree_with_remote`` takes when local is ahead by the
    # contract commit and remote is ahead by the agent's draft commit).
    _git("rebase", f"origin/{branch}", cwd=worktree)

    assert contract_path.read_text() == expected_body
    assert "decision-1" in contract_path.read_text()
    # The agent's draft commit from origin must also be present after rebase.
    assert (worktree / ".egg-state/drafts/42-analysis.md").exists()


def test_pre_sync_commit_survives_reset_to_origin_after_push(
    tmp_path: Path,
) -> None:
    """Even when the post-rebase flow falls through to a ``git reset
    --hard origin/<branch>`` (the local-ahead-then-push path inside
    ``_sync_worktree_with_remote``), the contract survives because the
    commit it sits on has already been pushed and is part of
    ``origin/<branch>``.
    """
    worktree, remote, branch, identifier = _setup_repo_with_remote(tmp_path)
    _push_agent_draft_to_origin(remote, branch, tmp_path)
    _git("fetch", "origin", cwd=worktree)

    contract_path = _agent_writes_decision_to_contract(worktree, identifier)

    _commit_statefiles_to_worktree(
        worktree,
        "Persist agent statefile writes before refine sync",
        pipeline_identifier=42,
        pipeline_id=identifier,
    )
    _git("rebase", f"origin/{branch}", cwd=worktree)
    _git("push", "origin", branch, cwd=worktree)

    # Re-fetch and reset (mirrors the post-push reset in the sync helper).
    _git("fetch", "origin", cwd=worktree)
    _git("reset", "--hard", f"origin/{branch}", cwd=worktree)

    assert "decision-1" in contract_path.read_text()
