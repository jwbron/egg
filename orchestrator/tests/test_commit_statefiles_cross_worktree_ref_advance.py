"""Regression test for #2626: pre-sync commit must not delete agent-pushed work.

`_commit_statefiles_to_worktree` runs at every phase boundary to capture the
orchestrator's live ``.egg-state/`` writes (contract mutations from MCP calls,
etc.) before the post-phase ``_sync_worktree_with_remote`` step.  In the
#2626 failure shape the helper instead landed a commit that *deleted* the
agent-pushed plan draft and agent-outputs that BRC consensus had just
produced.

The mechanism is a concurrency hazard between two unrelated subsystems:

1. ``sandbox/agent-config/rules/branch-recovery.md`` (also surfaced by the
   gateway's detached-HEAD commit hint) teaches agents to run
   ``git update-ref refs/heads/<assigned_branch> <sha>`` after a detached
   commit so their work lands on the assigned branch.  The gateway
   allowlists this exact form.
2. The orchestrator's pipeline worktree is a sibling ``git worktree add``
   of the same bare repo as the per-agent worktrees, and it has the
   pipeline branch checked out.  ``git update-ref`` is plumbing and does
   **not** respect per-worktree branch locks, so the agent's call advances
   the orchestrator-checked-out ref out from under the orchestrator
   worktree.  HEAD silently moves to the agent's commit; the index and
   working tree stay at the prior state.

After the cross-worktree advance, ``git status`` in the orchestrator's
worktree reports every agent-pushed file under ``.egg-state/`` as a
*staged deletion* (the index disagrees with HEAD) and a *working-tree
deletion* (the file was never written to disk in this worktree).  The
helper's prior shape — ``git add --force <hits>`` plus
``git commit -m … -- .egg-state/`` — committed both: the index disagreement
because it was already staged, and the working-tree disagreement because
``git commit -- <pathspec>`` defaults to ``--only`` semantics that
auto-stage working-tree changes for the named paths.

Fix (in :func:`_commit_statefiles_to_worktree`):

* ``git read-tree HEAD`` at function entry to refresh the index against
  HEAD without touching the working tree, so the cross-worktree advance
  no longer leaves the index showing the agent's files as staged
  deletions.
* Drop the ``-- .egg-state/`` pathspec from the final ``git commit`` so
  no working-tree changes are implicitly auto-staged.

These tests use real ``git worktree`` plumbing (no subprocess mocks) so
the shared-ref hazard is exercised end-to-end against ground truth.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

from routes.pipelines import _commit_statefiles_to_worktree


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


def _setup_shared_worktrees(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Build a bare repo with an orchestrator worktree and an agent worktree.

    Both worktrees are created via ``git worktree add`` against the same
    bare repo, so they share the ``refs/`` namespace — which is what makes
    the cross-worktree ``update-ref`` hazard reproducible.  The
    orchestrator worktree is checked out to ``<branch>`` (the pipeline
    branch).  The agent worktree is checked out to its own per-role
    branch (``egg/agent-A/work``) but its pushes target ``<branch>``,
    mirroring production where the gateway rewrites push refspecs.

    Returns ``(orch_wt, agent_wt, branch, identifier)``.
    """
    identifier = "issue-42"
    branch = f"egg/{identifier}/work"

    # --- bare remote ---
    remote = tmp_path / "remote.git"
    _git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)

    # --- seed origin/main ---
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-b", "main", cwd=seed)
    _git("config", "user.email", "test@example.com", cwd=seed)
    _git("config", "user.name", "test", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-m", "seed", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)

    # --- bare main-repo ("the gateway's /repos/<repo>/") cloned from remote ---
    main_repo = tmp_path / "main-repo"
    _git("clone", "--bare", str(remote), str(main_repo), cwd=tmp_path)
    _git(
        "config",
        "remote.origin.fetch",
        "+refs/heads/*:refs/remotes/origin/*",
        cwd=main_repo,
    )
    _git("fetch", "origin", cwd=main_repo)

    # --- orchestrator worktree, checked out to the pipeline branch ---
    orch_wt = tmp_path / "wt-orch"
    _git("worktree", "add", "-b", branch, str(orch_wt), "origin/main", cwd=main_repo)
    _git("config", "user.email", "orch@example.com", cwd=orch_wt)
    _git("config", "user.name", "orch", cwd=orch_wt)

    # Seed the orchestrator's bootstrap-contract commit (matching what the
    # pipeline does on its first checkpoint, so the contract path is tracked).
    contracts_dir = orch_wt / ".egg-state" / "contracts"
    contracts_dir.mkdir(parents=True)
    (contracts_dir / f"{identifier}.json").write_text(
        '{"schema_version":"1.0","pipeline_id":"' + identifier + '",'
        '"current_phase":"refine","decisions":[]}\n'
    )
    _git("add", ".egg-state/", cwd=orch_wt)
    _git("commit", "-m", "bootstrap contract", cwd=orch_wt)
    _git("push", "-u", "origin", branch, cwd=orch_wt)

    # --- agent worktree, checked out to its own per-role branch ---
    _git("fetch", "origin", cwd=main_repo)
    agent_wt = tmp_path / "wt-agent"
    _git(
        "worktree",
        "add",
        "-b",
        "egg/agent-A/work",
        str(agent_wt),
        f"origin/{branch}",
        cwd=main_repo,
    )
    _git("config", "user.email", "agent@example.com", cwd=agent_wt)
    _git("config", "user.name", "agent", cwd=agent_wt)

    return orch_wt, agent_wt, branch, identifier


def _agent_pushes_draft_and_runs_update_ref(agent_wt: Path, branch: str, identifier: str) -> str:
    """Simulate the agent's BRC-cycle propose + post-commit recovery.

    Mirrors what an agent does when a producer cycle ends with a detached
    HEAD: commit locally, push HEAD to the assigned branch, then run the
    gateway-allowed recovery primitive ``git update-ref refs/heads/<branch>
    HEAD`` to set the local assigned-branch ref to the commit.  See
    ``sandbox/agent-config/rules/branch-recovery.md``.

    Returns the agent's commit SHA so callers can assert against it.
    """
    drafts_dir = agent_wt / ".egg-state" / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    (drafts_dir / f"{identifier}-plan.md").write_text("# yaml-tasks\n\nagent-produced plan body\n")
    outputs_dir = agent_wt / ".egg-state" / "agent-outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    (outputs_dir / f"{identifier}-architect-output.json").write_text('{"role":"architect"}\n')
    _git("add", ".egg-state/", cwd=agent_wt)
    _git("commit", "-m", "agent: plan + architect output", cwd=agent_wt)
    sha = _git("rev-parse", "HEAD", cwd=agent_wt).stdout.strip()
    _git("push", "origin", f"HEAD:refs/heads/{branch}", cwd=agent_wt)
    # The gateway-allowed recovery primitive — sets refs/heads/<branch>
    # to the agent's HEAD.  Because the orchestrator's worktree has
    # refs/heads/<branch> checked out, this silently advances the
    # orchestrator worktree's HEAD (the symref) without touching its
    # index or working tree.
    _git("update-ref", f"refs/heads/{branch}", sha, cwd=agent_wt)
    return sha


def test_pre_sync_commit_does_not_delete_agent_files_after_cross_worktree_update_ref(
    tmp_path: Path,
) -> None:
    """The regression: pre-sync commit must preserve agent-pushed files.

    Reproduces the #2626 sequence end-to-end with real git plumbing:

    1. Orchestrator's worktree is set up, contract bootstrapped.
    2. Agent's worktree commits a plan draft + agent-output, pushes to
       the pipeline branch, then runs ``git update-ref`` to align its
       local ref — silently advancing the orchestrator worktree's HEAD.
    3. Orchestrator's MCP also modifies the contract live (the legitimate
       work the pre-sync commit is supposed to capture).
    4. ``_commit_statefiles_to_worktree`` runs.

    With the fix, the resulting commit captures the contract change and
    NOTHING ELSE — the agent-pushed files remain reachable from HEAD.
    Without either half of the fix, the commit either stages the
    index-vs-HEAD disagreement (``read-tree HEAD`` missing) or
    auto-stages the working-tree gap (``-- <pathspec>`` on commit) and
    lands a delete of the agent files (the symptom in #2626).
    """
    orch_wt, agent_wt, branch, identifier = _setup_shared_worktrees(tmp_path)
    agent_sha = _agent_pushes_draft_and_runs_update_ref(agent_wt, branch, identifier)

    # Sanity-check the pathological state: orchestrator's HEAD has silently
    # advanced to the agent's commit (via the shared branch ref), but the
    # index and working tree are stale.
    orch_head = _git("rev-parse", "HEAD", cwd=orch_wt).stdout.strip()
    assert orch_head == agent_sha, (
        "cross-worktree update-ref should have advanced orch worktree HEAD; "
        f"got {orch_head} expected {agent_sha}"
    )
    assert not (orch_wt / ".egg-state" / "drafts" / f"{identifier}-plan.md").exists(), (
        "orch worktree should NOT have the agent's plan draft on disk — that "
        "is the precondition of the bug"
    )

    # The orchestrator's MCP modifies the contract live during the phase.
    contract_path = orch_wt / ".egg-state" / "contracts" / f"{identifier}.json"
    contract_path.write_text(
        '{"schema_version":"1.0","pipeline_id":"' + identifier + '",'
        '"current_phase":"refine","decisions":[{"id":"decision-1",'
        '"question":"Which database?","type":"hitl","phase":"refine",'
        '"options":[{"id":"opt-1","label":"Postgres","description":null}],'
        '"resolved":false,"resolution":null,"resolved_by":null,'
        '"resolved_at":null,"debounce_until":null}]}\n'
    )

    # Run the production helper under test.
    committed = _commit_statefiles_to_worktree(
        orch_wt,
        "Persist agent statefile writes before plan sync",
        pipeline_identifier=identifier,
        pipeline_id=identifier,
    )

    assert committed is True, "expected a commit for the contract modification"

    # The agent-pushed files must still be reachable from HEAD — the
    # commit must not have deleted them.
    head_files = _git("ls-tree", "-r", "--name-only", "HEAD", cwd=orch_wt).stdout
    assert f".egg-state/drafts/{identifier}-plan.md" in head_files, (
        "regression: pre-sync commit deleted the agent's plan draft from HEAD "
        "(this is the #2626 symptom)"
    )
    assert f".egg-state/agent-outputs/{identifier}-architect-output.json" in head_files, (
        "regression: pre-sync commit deleted the agent's architect-output "
        "from HEAD (this is the #2626 symptom)"
    )

    # The contract modification IS expected in the commit.
    show = _git("show", "--name-status", "--format=", "HEAD", cwd=orch_wt).stdout
    assert f"M\t.egg-state/contracts/{identifier}.json" in show, (
        "expected the commit to include the contract modification\n" + show
    )

    # And the commit MUST NOT include deletions for any agent-pushed paths.
    assert ".egg-state/drafts/" not in show.replace(
        f"M\t.egg-state/drafts/{identifier}-plan.md", ""
    ).replace(f"A\t.egg-state/drafts/{identifier}-plan.md", ""), (
        "commit unexpectedly touched drafts/:\n" + show
    )
    assert ".egg-state/agent-outputs/" not in show, (
        "commit unexpectedly touched agent-outputs/:\n" + show
    )


def test_pre_sync_commit_idempotent_when_no_orch_writes_after_cross_worktree_advance(
    tmp_path: Path,
) -> None:
    """No orch-side writes + cross-worktree advance ⇒ helper must be a no-op.

    Without the fix, this case would still produce a delete commit (the
    cross-worktree advance is enough to make the index disagree with HEAD,
    and ``git commit -- <pathspec>`` would commit the disagreement).  With
    the fix, the helper sees ``git diff --cached --quiet`` exit zero after
    ``read-tree HEAD`` and short-circuits without creating a commit.
    """
    orch_wt, agent_wt, branch, identifier = _setup_shared_worktrees(tmp_path)
    agent_sha = _agent_pushes_draft_and_runs_update_ref(agent_wt, branch, identifier)

    head_before = _git("rev-parse", "HEAD", cwd=orch_wt).stdout.strip()
    assert head_before == agent_sha

    committed = _commit_statefiles_to_worktree(
        orch_wt,
        "Persist agent statefile writes before plan sync",
        pipeline_identifier=identifier,
        pipeline_id=identifier,
    )

    assert committed is False, "no orchestrator writes happened — helper must not produce a commit"
    head_after = _git("rev-parse", "HEAD", cwd=orch_wt).stdout.strip()
    assert head_after == head_before, (
        f"no commit expected, but HEAD moved from {head_before} to {head_after}"
    )
