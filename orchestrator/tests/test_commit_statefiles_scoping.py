"""Tests for _commit_statefiles_to_worktree pipeline scoping (#1390).

Verifies that when a pipeline_identifier is provided, only state files
belonging to that pipeline are staged and committed — preventing
concurrent pipelines from leaking state into each other's PRs.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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

from routes.pipelines import _commit_statefiles_to_worktree


def _make_run_side_effect(*, diff_has_changes: bool = True):
    """Create a side_effect for subprocess.run that simulates git behavior.

    When *diff_has_changes* is True, the ``git diff --cached --quiet``
    call returns non-zero (meaning there are staged changes to commit).
    """

    def _side_effect(cmd, **kwargs):
        result = MagicMock()
        # git diff --cached --quiet returns 0 when nothing staged, 1 when staged
        if "--quiet" in cmd:
            result.returncode = 1 if diff_has_changes else 0
        else:
            result.returncode = 0
        # ``_restore_missing_state_files_from_head`` invokes
        # ``ls-files -z --deleted`` in binary mode (no ``text=True``) so
        # NUL-separated output survives ``core.quotePath`` re-encoding;
        # the probe is therefore the one call whose stdout the helper
        # ``.split(b"\0")`` parses.  Return bytes for that single call
        # shape so the mock matches the helper's binary contract; every
        # other call still uses string stdout/stderr to keep the
        # existing assertions valid.
        if isinstance(cmd, list) and "ls-files" in cmd and "-z" in cmd and "--deleted" in cmd:
            result.stdout = b""
            result.stderr = b""
        else:
            result.stdout = ""
            result.stderr = ""
        return result

    return _side_effect


class TestCommitStatefilesScoping:
    """Scoped staging: only files matching pipeline_identifier are committed."""

    def test_scoped_stages_only_matching_files(self, tmp_path: Path):
        """With pipeline_identifier=42, only files containing '42' are staged."""
        # Create state files for pipeline 42 and pipeline 99
        for subdir in ("contracts", "drafts", "reviews", "agent-outputs"):
            d = tmp_path / ".egg-state" / subdir
            d.mkdir(parents=True, exist_ok=True)

        (tmp_path / ".egg-state" / "contracts" / "42.json").write_text("{}")
        (tmp_path / ".egg-state" / "drafts" / "42-plan.md").write_text("plan")
        (tmp_path / ".egg-state" / "reviews" / "42-implement-code-review.json").write_text("{}")
        (tmp_path / ".egg-state" / "contracts" / "99.json").write_text("{}")
        (tmp_path / ".egg-state" / "drafts" / "99-plan.md").write_text("other plan")

        with patch("subprocess.run", side_effect=_make_run_side_effect()) as mock_run:
            _commit_statefiles_to_worktree(tmp_path, "scoped commit", pipeline_identifier=42)

        # Find the git add call
        add_call = None
        for c in mock_run.call_args_list:
            cmd = c[0][0]
            if "add" in cmd and "--" in cmd:
                add_call = cmd
                break

        assert add_call is not None, "Expected a git add call"

        # --force must be present to override .gitignore (#1548)
        assert "--force" in add_call, (
            "git add must use --force to stage gitignored .egg-state/ files"
        )

        # The add call should contain paths with '42' but NOT '99'
        add_paths = add_call[add_call.index("--") + 1 :]
        add_filenames = [Path(p).name for p in add_paths]
        assert any("42" in name for name in add_filenames)
        assert not any("99" in name for name in add_filenames)

    def test_scoped_does_not_substring_match(self, tmp_path: Path):
        """Pipeline 4 must NOT match files for pipeline 42 (prefix-anchored)."""
        for subdir in ("contracts", "drafts"):
            d = tmp_path / ".egg-state" / subdir
            d.mkdir(parents=True, exist_ok=True)

        # Files for pipeline 4 (should match)
        (tmp_path / ".egg-state" / "contracts" / "4.json").write_text("{}")
        (tmp_path / ".egg-state" / "drafts" / "4-plan.md").write_text("plan")
        # Files for pipeline 42 (should NOT match)
        (tmp_path / ".egg-state" / "contracts" / "42.json").write_text("{}")
        (tmp_path / ".egg-state" / "drafts" / "42-plan.md").write_text("other")
        # Files for pipeline 142 (should NOT match)
        (tmp_path / ".egg-state" / "contracts" / "142.json").write_text("{}")

        with patch("subprocess.run", side_effect=_make_run_side_effect()) as mock_run:
            _commit_statefiles_to_worktree(tmp_path, "scoped commit", pipeline_identifier=4)

        add_call = None
        for c in mock_run.call_args_list:
            cmd = c[0][0]
            if "add" in cmd and "--" in cmd:
                add_call = cmd
                break

        assert add_call is not None, "Expected a git add call"
        add_paths = add_call[add_call.index("--") + 1 :]
        add_filenames = [Path(p).name for p in add_paths]
        # Should match pipeline 4 files only
        assert any(name.startswith("4.") or name.startswith("4-") for name in add_filenames)
        assert not any(name.startswith("42") for name in add_filenames)
        assert not any(name.startswith("142") for name in add_filenames)

    def test_scoped_no_matching_files_is_noop(self, tmp_path: Path):
        """When no files match the identifier, nothing is committed."""
        d = tmp_path / ".egg-state" / "contracts"
        d.mkdir(parents=True)
        (d / "99.json").write_text("{}")

        with patch("subprocess.run") as mock_run:
            _commit_statefiles_to_worktree(tmp_path, "noop commit", pipeline_identifier=42)

        # subprocess.run should never be called (no matching files)
        mock_run.assert_not_called()

    def test_none_identifier_stages_everything(self, tmp_path: Path):
        """With pipeline_identifier=None, all .egg-state/ files are staged (fallback)."""
        d = tmp_path / ".egg-state" / "contracts"
        d.mkdir(parents=True)
        (d / "42.json").write_text("{}")
        (d / "99.json").write_text("{}")

        with patch("subprocess.run", side_effect=_make_run_side_effect()) as mock_run:
            _commit_statefiles_to_worktree(tmp_path, "unscoped commit", pipeline_identifier=None)

        # The git add call should use the broad ".egg-state/" path with --force.
        # Locate by subcommand rather than position — the helper now also
        # runs ``git read-tree HEAD`` ahead of the add to defend against
        # cross-worktree branch-ref advance (#2626).
        add_call = next(
            (c for c in mock_run.call_args_list if "add" in c[0][0]),
            None,
        )
        assert add_call is not None, "Expected a git add call"
        cmd = add_call[0][0]
        assert ".egg-state/" in cmd
        assert "--force" in cmd, "git add must use --force to stage gitignored .egg-state/ files"

    def test_idempotent_when_nothing_staged(self, tmp_path: Path):
        """When diff --cached --quiet returns 0, no commit is created."""
        d = tmp_path / ".egg-state" / "contracts"
        d.mkdir(parents=True)
        (d / "42.json").write_text("{}")

        with patch(
            "subprocess.run", side_effect=_make_run_side_effect(diff_has_changes=False)
        ) as mock_run:
            _commit_statefiles_to_worktree(tmp_path, "idempotent", pipeline_identifier=42)

        # Should have add + diff calls, but NOT a commit call
        commit_calls = [c for c in mock_run.call_args_list if "commit" in c[0][0]]
        assert len(commit_calls) == 0

    def test_string_pipeline_identifier(self, tmp_path: Path):
        """Works with string pipeline IDs (no issue number)."""
        d = tmp_path / ".egg-state" / "contracts"
        d.mkdir(parents=True)
        (d / "pipe-abc.json").write_text("{}")
        (d / "pipe-xyz.json").write_text("{}")

        with patch("subprocess.run", side_effect=_make_run_side_effect()) as mock_run:
            _commit_statefiles_to_worktree(
                tmp_path, "scoped string", pipeline_identifier="pipe-abc"
            )

        add_call = None
        for c in mock_run.call_args_list:
            cmd = c[0][0]
            if "add" in cmd and "--" in cmd:
                add_call = cmd
                break

        assert add_call is not None
        add_paths = add_call[add_call.index("--") + 1 :]
        add_paths_str = " ".join(add_paths)
        assert "pipe-abc" in add_paths_str
        assert "pipe-xyz" not in add_paths_str

    def test_no_state_dir_is_noop(self, tmp_path: Path):
        """When .egg-state/ doesn't exist, nothing happens."""
        with patch("subprocess.run") as mock_run:
            _commit_statefiles_to_worktree(tmp_path, "no dir", pipeline_identifier=42)

        mock_run.assert_not_called()


class TestCommitStatefilesPipelineIdUnion:
    """Union staging: issue_number-prefixed files AND pipeline_id-keyed files (#1829)."""

    def test_canonical_contract_file_staged_with_pipeline_id(self, tmp_path: Path):
        """Contract file keyed by pipeline_id is staged even when pipeline_identifier=issue_number.

        Regression for #1829: contract files are named ``{pipeline.id}.json``
        (e.g. ``issue-1759-v3.json``) while drafts are prefixed with the
        issue number (e.g. ``1759-plan.md``).  Passing both identifiers
        ensures both prefixes match.
        """
        for subdir in ("contracts", "drafts"):
            (tmp_path / ".egg-state" / subdir).mkdir(parents=True, exist_ok=True)

        (tmp_path / ".egg-state" / "contracts" / "issue-1759-v3.json").write_text("{}")
        (tmp_path / ".egg-state" / "drafts" / "1759-plan.md").write_text("plan")

        with patch("subprocess.run", side_effect=_make_run_side_effect()) as mock_run:
            _commit_statefiles_to_worktree(
                tmp_path,
                "plan-phase commit",
                pipeline_identifier=1759,
                pipeline_id="issue-1759-v3",
            )

        add_call = None
        for c in mock_run.call_args_list:
            cmd = c[0][0]
            if "add" in cmd and "--" in cmd:
                add_call = cmd
                break

        assert add_call is not None, "Expected a git add call"
        add_paths = add_call[add_call.index("--") + 1 :]
        add_filenames = [Path(p).name for p in add_paths]

        assert "issue-1759-v3.json" in add_filenames, (
            "Contract file keyed by pipeline_id must be staged"
        )
        assert "1759-plan.md" in add_filenames, (
            "Draft file keyed by issue_number must still be staged"
        )

    def test_pipeline_id_only_stages_matching_contract(self, tmp_path: Path):
        """When only pipeline_id is provided (no issue_number), its prefix is used."""
        d = tmp_path / ".egg-state" / "contracts"
        d.mkdir(parents=True)
        (d / "pipeline-2d7b273f.json").write_text("{}")
        (d / "pipeline-abcd1234.json").write_text("{}")

        with patch("subprocess.run", side_effect=_make_run_side_effect()) as mock_run:
            _commit_statefiles_to_worktree(
                tmp_path,
                "pipeline-id-only",
                pipeline_id="pipeline-2d7b273f",
            )

        add_call = None
        for c in mock_run.call_args_list:
            cmd = c[0][0]
            if "add" in cmd and "--" in cmd:
                add_call = cmd
                break

        assert add_call is not None
        add_paths = add_call[add_call.index("--") + 1 :]
        add_filenames = [Path(p).name for p in add_paths]
        assert "pipeline-2d7b273f.json" in add_filenames
        assert "pipeline-abcd1234.json" not in add_filenames

    def test_duplicate_prefixes_deduplicated(self, tmp_path: Path):
        """Passing the same string as both identifiers doesn't double-stage."""
        d = tmp_path / ".egg-state" / "contracts"
        d.mkdir(parents=True)
        (d / "pipe-abc.json").write_text("{}")

        with patch("subprocess.run", side_effect=_make_run_side_effect()) as mock_run:
            _commit_statefiles_to_worktree(
                tmp_path,
                "dedupe",
                pipeline_identifier="pipe-abc",
                pipeline_id="pipe-abc",
            )

        add_call = None
        for c in mock_run.call_args_list:
            cmd = c[0][0]
            if "add" in cmd and "--" in cmd:
                add_call = cmd
                break

        assert add_call is not None
        add_paths = add_call[add_call.index("--") + 1 :]
        # File should appear exactly once
        assert add_paths.count(str(Path(".egg-state/contracts/pipe-abc.json"))) == 1


class TestCommitStatefilesNoAutoStageDeletions:
    """Regression for #2625: commit must not auto-stage working-tree deletions.

    Agents push their drafts to ``origin/<branch>`` from their own
    worktrees, so the orchestrator's local checkout can sit at a HEAD
    that contains a draft file even when the file was never materialised
    on disk locally. Prior to the fix, ``git commit -- .egg-state/``
    auto-staged the apparent "deletion" of those files, wiping
    agent-authored drafts off the work branch.
    """

    def _init_repo(self, tmp_path: Path):
        """Create a real git repo seeded with an initial commit."""
        git_base = ["git", "-C", str(tmp_path)]
        subprocess.run([*git_base, "init", "-q"], check=True)
        subprocess.run([*git_base, "config", "user.email", "test@example.com"], check=True)
        subprocess.run([*git_base, "config", "user.name", "Test"], check=True)
        # Disable commit signing and any globally-configured hooksPath
        # so the test does not depend on the developer's global git
        # config (no signing key or a hooks dir with failing hooks
        # would otherwise break the seed commit).
        subprocess.run([*git_base, "config", "commit.gpgsign", "false"], check=True)
        subprocess.run([*git_base, "config", "core.hooksPath", "/dev/null"], check=True)
        return git_base

    def test_does_not_commit_deletion_of_file_missing_from_worktree(self, tmp_path: Path):
        """File in HEAD but missing from worktree is NOT committed as deleted (#2625)."""
        git_base = self._init_repo(tmp_path)

        drafts = tmp_path / ".egg-state" / "drafts"
        contracts = tmp_path / ".egg-state" / "contracts"
        drafts.mkdir(parents=True)
        contracts.mkdir(parents=True)

        # Seed HEAD with both a draft and a contract.
        (drafts / "issue-99-v2-analysis.md").write_text("agent analysis", encoding="utf-8")
        (contracts / "issue-99-v2.json").write_text("{}", encoding="utf-8")
        subprocess.run([*git_base, "add", "-A"], check=True, capture_output=True)
        subprocess.run([*git_base, "commit", "-q", "-m", "seed"], check=True, capture_output=True)

        head_before = subprocess.run(
            [*git_base, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Simulate the live bug: orchestrator has the agent's commit in
        # HEAD (draft tracked) but the local worktree is missing the
        # draft file. The orchestrator-side mutation modifies the
        # contract on disk.
        (drafts / "issue-99-v2-analysis.md").unlink()
        (contracts / "issue-99-v2.json").write_text(
            '{"orchestrator": "wrote this"}', encoding="utf-8"
        )

        committed = _commit_statefiles_to_worktree(
            tmp_path,
            "Persist agent statefile writes before refine sync",
            pipeline_identifier="issue-99-v2",
            pipeline_id="issue-99-v2",
        )
        assert committed is True

        head_after = subprocess.run(
            [*git_base, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head_after != head_before, "Expected a new commit on top of HEAD"

        # The draft file MUST still be in the new commit's tree — the
        # bug was that the commit silently dropped it.
        ls = subprocess.run(
            [
                *git_base,
                "ls-tree",
                "HEAD",
                "--",
                ".egg-state/drafts/issue-99-v2-analysis.md",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert ls.stdout.strip(), (
            "Draft file present in HEAD but missing from worktree was "
            "silently committed as deleted (#2625 regression)"
        )

        # Sanity: the orchestrator's contract mutation IS in the commit.
        show = subprocess.run(
            [*git_base, "show", "--name-only", "--format=", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        committed_files = set(show.stdout.strip().splitlines())
        assert ".egg-state/contracts/issue-99-v2.json" in committed_files

    def test_unrelated_unstaged_egg_state_deletion_is_not_committed(self, tmp_path: Path):
        """An unstaged deletion of a different pipeline's file is not picked up."""
        git_base = self._init_repo(tmp_path)

        drafts = tmp_path / ".egg-state" / "drafts"
        contracts = tmp_path / ".egg-state" / "contracts"
        drafts.mkdir(parents=True)
        contracts.mkdir(parents=True)

        # Two pipelines coexist in HEAD.
        (drafts / "issue-99-v2-analysis.md").write_text("ours", encoding="utf-8")
        (drafts / "issue-77-analysis.md").write_text("theirs", encoding="utf-8")
        (contracts / "issue-99-v2.json").write_text("{}", encoding="utf-8")
        subprocess.run([*git_base, "add", "-A"], check=True, capture_output=True)
        subprocess.run([*git_base, "commit", "-q", "-m", "seed"], check=True, capture_output=True)

        # The OTHER pipeline's draft is missing from the worktree (e.g.
        # orchestrator restarted mid-flight and only one pipeline's
        # files were rehydrated). The scoped commit for issue-99-v2
        # must not collateral-damage issue-77's draft.
        (drafts / "issue-77-analysis.md").unlink()
        (contracts / "issue-99-v2.json").write_text('{"updated": true}', encoding="utf-8")

        _commit_statefiles_to_worktree(
            tmp_path,
            "scoped commit",
            pipeline_identifier="issue-99-v2",
            pipeline_id="issue-99-v2",
        )

        ls = subprocess.run(
            [
                *git_base,
                "ls-tree",
                "HEAD",
                "--",
                ".egg-state/drafts/issue-77-analysis.md",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert ls.stdout.strip(), (
            "Unrelated pipeline's draft was deleted by a scoped commit for a different pipeline"
        )
