"""Module-level helpers for regression integration tests.

Houses both:

* The git-plumbing / worktree-builder helpers used by the salvage and
  recovery-invariant suites (``test_unpushed_commit_salvage.py``,
  ``test_recovery_invariants.py``) — they exercise the agent-salvage
  chain on real on-disk worktrees, and the worktree shape needs to be
  uniform so the tests document the same contract.
* The BRC consensus helpers (``make_tracker``, ``propose_payload``,
  ``filter_events``, …) used by the BRC regression suites (issue
  #2635). They live next to ``conftest.py`` because pytest's conftest
  discovery does NOT make conftest's module-level symbols importable
  from sibling test modules — only fixtures (declared with
  ``@pytest.fixture``) are. Splitting plain helpers into their own
  module keeps both available without contorting them into fixtures.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Mirror conftest.py's path setup so this module can be imported
# stand-alone (pytest collects conftest before test modules, but
# IDEs / type-checkers don't).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (
    _PROJECT_ROOT / "orchestrator",
    _PROJECT_ROOT / "shared",
    _PROJECT_ROOT,
):
    if _p.exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agent_salvage import AgentWorktree  # noqa: E402
from events import Event, EventType  # noqa: E402
from peer_consensus import (  # noqa: E402
    PeerConsensusTracker,
    create_peer_consensus_tracker,
    remove_peer_consensus_tracker,
)
from review_graph import ReviewGraph  # noqa: E402


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git`` with a regression-test identity, hooks disabled."""
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.email=salvage@test.example",
            "-c",
            "user.name=Salvage Tester",
            "-C",
            str(cwd),
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def make_repo(path: Path, branch_name: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "--initial-branch", branch_name, cwd=path)
    (path / "README.md").write_text("seed\n")
    git("add", "README.md", cwd=path)
    git("commit", "-q", "-m", "seed", cwd=path)
    return git("rev-parse", "HEAD", cwd=path).stdout.strip()


def commit(path: Path, filename: str, content: str, message: str) -> str:
    (path / filename).write_text(content)
    git("add", filename, cwd=path)
    git("commit", "-q", "-m", message, cwd=path)
    return git("rev-parse", "HEAD", cwd=path).stdout.strip()


def set_assigned_branch(repo: Path, local_branch: str, assigned: str) -> None:
    """Mirror what gateway/worktree_manager writes at worktree create time.

    The agent's per-agent worktree carries ``branch.<local>.merge`` set
    to ``refs/heads/<assigned>``; that's how ``list_unpushed_commits``
    discovers the assigned upstream when computing the anchor cut.
    """
    git(
        "config",
        f"branch.{local_branch}.merge",
        f"refs/heads/{assigned}",
        cwd=repo,
    )


def create_origin_tracking(repo: Path, remote_branch: str, sha: str) -> None:
    """Stand in for ``origin/<branch>`` after a fetch."""
    git("update-ref", f"refs/remotes/origin/{remote_branch}", sha, cwd=repo)


def build_worktree(
    base: Path,
    pipeline_id: str,
    *,
    agent_role: str | None,
    slice_id: str | None,
    assigned_branch: str,
    n_unpushed: int = 1,
) -> tuple[AgentWorktree, str]:
    """Create a per-agent worktree directory with ``n_unpushed`` local commits.

    Returns the ``AgentWorktree`` descriptor and the HEAD SHA (the SHA
    that the salvage path is supposed to push to its recovery ref).
    """
    if agent_role is None:
        worktree_id = pipeline_id
        scope = "pipeline"
    elif slice_id is None:
        worktree_id = f"{pipeline_id}-{agent_role}"
        scope = agent_role
    else:
        worktree_id = f"{pipeline_id}-{slice_id}-{agent_role}"
        scope = f"{slice_id}-{agent_role}"

    local_branch = f"egg/{worktree_id}/work"
    repo = base / worktree_id / "repo"
    anchor = make_repo(repo, local_branch)
    set_assigned_branch(repo, local_branch, assigned_branch)
    # The wedged scenario from #2429: agent pushes were rejected so the
    # assigned-branch tracking ref never advanced past the anchor. Local
    # commits accumulate on the work branch with no remote anchor for
    # them.
    create_origin_tracking(repo, assigned_branch, anchor)

    head = anchor
    for i in range(n_unpushed):
        head = commit(repo, f"unpushed-{i}.txt", f"work {i}\n", f"unpushed change {i}")

    wt = AgentWorktree(
        worktree_id=worktree_id,
        pipeline_id=pipeline_id,
        agent_role=agent_role,
        slice_id=slice_id,
        repo_path=repo,
        local_branch=local_branch,
    )
    assert wt.scope_label == scope  # belt-and-braces against helper drift
    return wt, head


def make_tracker(
    pipeline_id: str,
    graph: ReviewGraph,
    *,
    cooldown_seconds: int = 0,
) -> PeerConsensusTracker:
    """Build a tracker with all agents registered (cooldown=0 for tests).

    Registers the tracker in the module-level registry via
    ``create_peer_consensus_tracker`` so ``get_peer_consensus_tracker``
    — the API consumed by ``_handle_brc_consensus_timeout`` and the
    SSE bridge — finds it.  A direct ``PeerConsensusTracker(...)``
    constructor call would be a registry-bypass and break the
    timeout-handler test scenarios silently (the handler's
    ``_brc_tracker = get_peer_consensus_tracker(...)`` returns
    ``None`` and the alert paths short-circuit).
    """
    # Reset any tracker the previous test left under the same id —
    # ``create_peer_consensus_tracker`` overwrites silently, but
    # explicit removal also clears any per-agent state that survives
    # across re-creation.
    remove_peer_consensus_tracker(pipeline_id)
    tracker = create_peer_consensus_tracker(pipeline_id, graph, cooldown_seconds=cooldown_seconds)
    for role in graph.all_roles():
        tracker.register_agent(role)
    return tracker


def propose_payload(
    *,
    artifacts: list[str] | None = None,
    commit_sha: str = "abc1234",
    summary: str = "test proposal",
) -> dict[str, Any]:
    """Minimal valid ``ProposalPayload`` dict (no attestation)."""
    return {
        "summary": summary,
        "artifacts": list(artifacts or ["a.py"]),
        "commit_sha": commit_sha,
    }


def ack_payload(*, artifacts: list[str] | None = None) -> dict[str, Any]:
    """Minimal valid ``ReviewPayload`` ACK dict."""
    return {"artifact_references": list(artifacts or ["a.py"])}


def nack_payload(
    *,
    artifacts: list[str] | None = None,
    reason: str = "regression in a.py:42",
) -> dict[str, Any]:
    """Minimal valid ``ReviewPayload`` NACK dict (reason is required)."""
    return {
        "artifact_references": list(artifacts or ["a.py"]),
        "reason": reason,
    }


def filter_events(
    events: list[Event],
    *,
    pipeline_id: str,
    event_type: EventType | None = None,
) -> list[Event]:
    """Return events for ``pipeline_id`` (and optional ``event_type``)."""
    out = [e for e in events if e.pipeline_id == pipeline_id]
    if event_type is not None:
        out = [e for e in out if e.event_type == event_type]
    return out


EventFilter = Callable[..., list[Event]]
