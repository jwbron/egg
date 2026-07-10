"""Tests for the evidence patch-id rescue (#3572).

The incident this covers: a coder completed its tasks citing its local
HEAD SHA, the final push then went through the gateway push
reconciliation which rebased the worktree onto the remote tip, and the
rewritten commits landed on the integration branch under new SHAs. The
#3125 evidence gate flagged the cited (pre-rebase) SHAs unreachable and
blocked a consensus-complete slice from ever closing.

These tests build real git repositories and reproduce the rewrite with
a cherry-pick (identical patch, new SHA; exactly what a clean rebase
produces), then verify the rescue maps the cited SHA to the on-branch
rewrite, and that every degradation path returns "no rescue" rather
than failing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))
_shared_path = _orchestrator_path.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

import evidence_rescue  # noqa: E402
from commit_authorship_store import CommitAuthorshipStore  # noqa: E402

PIPELINE_ID = "pipeline-rescue-test"
BRANCH = "egg/issue-3572/slice-2"


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def rebase_rewrite_repo(tmp_path: Path) -> dict[str, object]:
    """A repo reproducing the #3572 rewrite.

    * ``cited_sha``: the agent's local commit, cited in the task record.
    * ``rewritten_sha``: the same patch cherry-picked onto a diverged
      integration branch (what ``_reconcile_and_retry_push``'s rebase
      mints), which is what actually landed on the branch.
    * ``refs/remotes/origin/<BRANCH>`` points at the diverged tip, as
      the #3125 gate's fetch leaves it.

    The cited commit's object remains in the odb (the common case: the
    rebase rewrote the worktree branch but never pruned the old
    objects), so the odb-computed patch-id path is exercised.
    """
    repo = _init_repo(tmp_path / "repo")
    base = _commit_file(repo, "base.txt", "base\n", "base commit")

    # The agent's local work on top of base.
    cited_sha = _commit_file(repo, "feature.txt", "the deliverable\n", "add feature")

    # The remote diverged (another producer's consensus_push), then the
    # reconcile rebase replayed the agent's patch on top of it.
    _git(repo, "checkout", "-q", "-b", "integration", base)
    _commit_file(repo, "other.txt", "sibling work\n", "sibling producer work")
    _git(repo, "cherry-pick", cited_sha)
    rewritten_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", f"refs/remotes/origin/{BRANCH}", rewritten_sha)
    _git(repo, "checkout", "-q", "main")

    return {"repo": repo, "cited_sha": cited_sha, "rewritten_sha": rewritten_sha}


class TestPatchIdHelpers:
    def test_patch_id_for_commit(self, rebase_rewrite_repo: dict[str, object]) -> None:
        repo = rebase_rewrite_repo["repo"]
        cited = rebase_rewrite_repo["cited_sha"]
        rewritten = rebase_rewrite_repo["rewritten_sha"]
        pid_cited = evidence_rescue.patch_id_for_commit(repo, cited)
        pid_rewritten = evidence_rescue.patch_id_for_commit(repo, rewritten)
        assert pid_cited is not None
        # The rewrite carries the identical patch: same stable patch-id.
        assert pid_cited == pid_rewritten

    def test_patch_id_for_missing_object_is_none(
        self, rebase_rewrite_repo: dict[str, object]
    ) -> None:
        repo = rebase_rewrite_repo["repo"]
        assert evidence_rescue.patch_id_for_commit(repo, "1" * 40) is None

    def test_patch_id_outside_repo_is_none(self, tmp_path: Path) -> None:
        assert evidence_rescue.patch_id_for_commit(tmp_path, "1" * 40) is None

    def test_branch_patch_ids_maps_branch_commits(
        self, rebase_rewrite_repo: dict[str, object]
    ) -> None:
        repo = rebase_rewrite_repo["repo"]
        rewritten = rebase_rewrite_repo["rewritten_sha"]
        pid = evidence_rescue.patch_id_for_commit(repo, rewritten)
        mapping = evidence_rescue.branch_patch_ids(repo, f"refs/remotes/origin/{BRANCH}")
        assert mapping[pid] == rewritten

    def test_branch_patch_ids_unresolvable_ref(
        self, rebase_rewrite_repo: dict[str, object]
    ) -> None:
        repo = rebase_rewrite_repo["repo"]
        assert evidence_rescue.branch_patch_ids(repo, "refs/heads/nope") == {}


class TestRescueUnreachableCommits:
    def test_rebase_rewrite_is_rescued(self, rebase_rewrite_repo: dict[str, object]) -> None:
        repo = rebase_rewrite_repo["repo"]
        cited = rebase_rewrite_repo["cited_sha"]
        rewritten = rebase_rewrite_repo["rewritten_sha"]
        rescued = evidence_rescue.rescue_unreachable_commits(
            PIPELINE_ID,
            repo,
            unreachable_shas=[cited],
            integration_branch=BRANCH,
        )
        assert rescued == {cited: rewritten}

    def test_kill_switch_disables_rescue(
        self,
        rebase_rewrite_repo: dict[str, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(evidence_rescue.RESCUE_ENV_VAR, "off")
        rescued = evidence_rescue.rescue_unreachable_commits(
            PIPELINE_ID,
            rebase_rewrite_repo["repo"],
            unreachable_shas=[rebase_rewrite_repo["cited_sha"]],
            integration_branch=BRANCH,
        )
        assert rescued == {}

    def test_unresolvable_branch_yields_no_rescue(
        self, rebase_rewrite_repo: dict[str, object]
    ) -> None:
        rescued = evidence_rescue.rescue_unreachable_commits(
            PIPELINE_ID,
            rebase_rewrite_repo["repo"],
            unreachable_shas=[rebase_rewrite_repo["cited_sha"]],
            integration_branch="egg/issue-3572/never-fetched",
        )
        assert rescued == {}

    def test_non_repo_path_yields_no_rescue(self, tmp_path: Path) -> None:
        rescued = evidence_rescue.rescue_unreachable_commits(
            PIPELINE_ID,
            tmp_path,
            unreachable_shas=["a" * 40],
            integration_branch=BRANCH,
        )
        assert rescued == {}

    def test_divergent_content_is_not_rescued(self, rebase_rewrite_repo: dict[str, object]) -> None:
        """A cited commit whose patch never landed (e.g. a conflicted
        rebase changed the content) keeps the strict #3125 verdict.
        """
        repo = rebase_rewrite_repo["repo"]
        _git(repo, "checkout", "-q", "-b", "divergent")
        lost = _commit_file(repo, "lost.txt", "never pushed\n", "truly lost work")
        _git(repo, "checkout", "-q", "main")
        rescued = evidence_rescue.rescue_unreachable_commits(
            PIPELINE_ID,
            repo,
            unreachable_shas=[lost],
            integration_branch=BRANCH,
        )
        assert rescued == {}

    def test_partial_rescue(self, rebase_rewrite_repo: dict[str, object]) -> None:
        repo = rebase_rewrite_repo["repo"]
        cited = rebase_rewrite_repo["cited_sha"]
        rewritten = rebase_rewrite_repo["rewritten_sha"]
        _git(repo, "checkout", "-q", "-b", "divergent2")
        lost = _commit_file(repo, "lost2.txt", "never pushed\n", "truly lost work 2")
        _git(repo, "checkout", "-q", "main")
        rescued = evidence_rescue.rescue_unreachable_commits(
            PIPELINE_ID,
            repo,
            unreachable_shas=[cited, lost],
            integration_branch=BRANCH,
        )
        assert rescued == {cited: rewritten}

    def test_registry_fallback_when_object_pruned(
        self,
        rebase_rewrite_repo: dict[str, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The fully-lost variant: the cited SHA's object exists in no
        odb, but the authorship registry recorded its patch-id at
        commit time (#2932). The rescue recovers via the registry.
        """
        repo = rebase_rewrite_repo["repo"]
        rewritten = rebase_rewrite_repo["rewritten_sha"]
        pid = evidence_rescue.patch_id_for_commit(repo, rewritten)
        pruned_sha = "9" * 40  # object not in the odb

        monkeypatch.setattr(
            evidence_rescue,
            "_recorded_patch_ids",
            lambda shas: {pruned_sha: pid},
        )
        rescued = evidence_rescue.rescue_unreachable_commits(
            PIPELINE_ID,
            repo,
            unreachable_shas=[pruned_sha],
            integration_branch=BRANCH,
        )
        assert rescued == {pruned_sha: rewritten}

    def test_registry_unavailable_degrades(
        self,
        rebase_rewrite_repo: dict[str, object],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            evidence_rescue,
            "_recorded_patch_ids",
            lambda shas: {},
        )
        rescued = evidence_rescue.rescue_unreachable_commits(
            PIPELINE_ID,
            rebase_rewrite_repo["repo"],
            unreachable_shas=["9" * 40],
            integration_branch=BRANCH,
        )
        assert rescued == {}


class TestLookupPatchIds:
    """``CommitAuthorshipStore.lookup_patch_ids``: the sha to patch-id
    inverse the rescue's registry fallback consumes.
    """

    STORE_PIPELINE_ID = "issue-3572"

    def test_returns_recorded_patch_id(self, tmp_path: Path) -> None:
        store = CommitAuthorshipStore(worktree_dir=tmp_path, synchronous=True)
        sha = "a" * 40
        pid = "b" * 40
        store.register(sha, "coder", self.STORE_PIPELINE_ID, patch_id=pid, commit=False)
        assert store.lookup_patch_ids([sha]) == {sha: pid}

    def test_unregistered_and_v1_entries_map_to_none(self, tmp_path: Path) -> None:
        store = CommitAuthorshipStore(worktree_dir=tmp_path, synchronous=True)
        v1_sha = "c" * 40
        store.register(v1_sha, "coder", self.STORE_PIPELINE_ID, patch_id=None, commit=False)
        result = store.lookup_patch_ids([v1_sha, "d" * 40])
        assert result == {v1_sha: None, "d" * 40: None}

    def test_invalid_shas_dropped(self, tmp_path: Path) -> None:
        store = CommitAuthorshipStore(worktree_dir=tmp_path, synchronous=True)
        assert store.lookup_patch_ids(["not-a-sha", ""]) == {}

    def test_empty_input(self, tmp_path: Path) -> None:
        store = CommitAuthorshipStore(worktree_dir=tmp_path, synchronous=True)
        assert store.lookup_patch_ids([]) == {}
