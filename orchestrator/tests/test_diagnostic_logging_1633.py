"""Tests for diagnostic logging added in #1633.

Validates INFO-level logging for _write_brc_history, _commit_statefiles_to_worktree,
_rewrite_brc_history_for_pr, and the PR-phase push handler.
Also includes the integration test (task-1-5) verifying the full call chain in a
real git repo.
"""

import subprocess
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

# Mock heavy dependencies that pipelines.py imports at module level
_docker_mock = MagicMock()
sys.modules.setdefault("docker", _docker_mock)
sys.modules.setdefault("docker.errors", _docker_mock.errors)
sys.modules.setdefault("docker.types", _docker_mock.types)

from message_store import Message, MessageStore, MessageType
from models import PipelineStatus

# Default slice_id seeded onto implement-phase BRC messages so the
# post-#2548 hard-switchover writer accepts them.
_DEFAULT_IMPLEMENT_SLICE_ID = "slice-1"


def _make_brc_message(
    pipeline_id="issue-42",
    from_role="coder",
    message_type=MessageType.CONSENSUS_PROPOSE,
    subject="Proposal",
    body="test body",
    phase="implement",
    timestamp=None,
    slice_id="__default__",
):
    """Create a BRC message for testing.

    For implement-phase messages, ``metadata['slice_id']`` is auto-stamped
    to ``slice-1`` (#2548 hard switchover) unless ``slice_id`` is set
    explicitly (pass ``None`` to test the missing-slice_id drop path).
    """
    md: dict = {}
    if slice_id == "__default__":
        if phase == "implement":
            md["slice_id"] = _DEFAULT_IMPLEMENT_SLICE_ID
    elif slice_id is not None:
        md["slice_id"] = slice_id
    return Message(
        pipeline_id=pipeline_id,
        from_role=from_role,
        to_role="all",
        message_type=message_type,
        subject=subject,
        body=body,
        phase=phase,
        timestamp=timestamp or datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
        metadata=md,
    )


# ---------------------------------------------------------------------------
# _write_brc_history logging tests
# ---------------------------------------------------------------------------
class TestWriteBrcHistoryLogging:
    """Verify INFO-level diagnostic logging in _write_brc_history (#1633 task-1-1)."""

    def test_logs_entry_with_all_params(self, tmp_path):
        """Function entry logs pipeline_id, phase, and identifier at INFO level."""
        from routes.pipelines import _write_brc_history

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = []

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _write_brc_history(tmp_path, "issue-99", "implement", 99)

        # Entry log should include pipeline_id, phase, and identifier
        entry_calls = [c for c in mock_logger.info.call_args_list if "entering" in str(c)]
        assert len(entry_calls) >= 1, "Expected an entry log"
        entry_kwargs = entry_calls[0][1]
        assert entry_kwargs.get("pipeline_id") == "issue-99"
        assert entry_kwargs.get("phase") == "implement"
        assert entry_kwargs.get("identifier") == "99"

    def test_logs_info_when_message_store_unavailable(self, tmp_path):
        """Early return when message store is None logs at INFO (not DEBUG)."""
        from routes.pipelines import _write_brc_history

        # Patch _get_message_store to return None (simulates ImportError path)
        with (
            patch("routes.pipelines._get_message_store", return_value=None),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        # Should log at INFO, not DEBUG
        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("message store unavailable" in msg for msg in info_msgs), (
            f"Expected INFO log about unavailable message store, got: {info_msgs}"
        )
        # Ensure no DEBUG log for this path
        debug_msgs = [str(c) for c in mock_logger.debug.call_args_list]
        assert not any("message store" in msg.lower() for msg in debug_msgs), (
            "Message store unavailability should NOT log at DEBUG anymore"
        )

    def test_logs_warning_when_message_retrieval_fails(self, tmp_path):
        """Early return on message retrieval exception logs at WARNING."""
        from routes.pipelines import _write_brc_history

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.side_effect = Exception("Store error")

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        warning_msgs = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("failed to retrieve" in msg.lower() for msg in warning_msgs), (
            f"Expected WARNING log about failed retrieval, got: {warning_msgs}"
        )

    def test_logs_info_when_no_messages_in_store(self, tmp_path):
        """Early return when store returns empty list logs at INFO."""
        from routes.pipelines import _write_brc_history

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = []

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("no messages in store" in msg for msg in info_msgs), (
            f"Expected INFO log about no messages, got: {info_msgs}"
        )

    def test_logs_info_when_no_brc_messages_for_phase(self, tmp_path):
        """Early return when messages exist but none are BRC for the phase."""
        from routes.pipelines import _write_brc_history

        # Return a non-BRC message so the "no messages" check passes
        # but the "no BRC messages" check triggers
        non_brc = Message(
            pipeline_id="issue-42",
            from_role="coder",
            to_role="all",
            message_type="GENERAL",
            subject="Not a BRC message",
            body="",
            phase="implement",
            timestamp=datetime(2026, 4, 8, 12, 0, 0, tzinfo=UTC),
            metadata={},
        )

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = [non_brc]

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("no BRC messages for phase" in msg for msg in info_msgs), (
            f"Expected INFO log about no BRC messages, got: {info_msgs}"
        )
        # Should include total_messages count
        no_brc_call = [c for c in mock_logger.info.call_args_list if "no BRC messages" in str(c)]
        assert no_brc_call[0][1].get("total_messages") == 1

    def test_logs_exit_on_success(self, tmp_path):
        """Successful write logs file path and message count at INFO."""
        from routes.pipelines import _write_brc_history

        messages = [_make_brc_message(phase="implement")]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with (
            patch("message_store.get_message_store", return_value=mock_store),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("Wrote BRC history file" in msg for msg in info_msgs), (
            f"Expected exit log, got: {info_msgs}"
        )
        wrote_call = [c for c in mock_logger.info.call_args_list if "Wrote BRC history" in str(c)]
        assert wrote_call[0][1].get("message_count") == 1


# ---------------------------------------------------------------------------
# _commit_statefiles_to_worktree logging tests
# ---------------------------------------------------------------------------
class TestCommitStatefilesLogging:
    """Verify INFO-level diagnostic logging in _commit_statefiles_to_worktree (#1633 task-1-2)."""

    def test_logs_entering(self, tmp_path):
        """Entry log is emitted as the first log in the function."""
        from routes.pipelines import _commit_statefiles_to_worktree

        state_dir = tmp_path / ".egg-state"
        state_dir.mkdir(parents=True)
        (state_dir / "42.json").write_text("{}", encoding="utf-8")

        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _commit_statefiles_to_worktree(tmp_path, "Test commit", pipeline_identifier=42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("entering" in msg for msg in info_msgs), (
            f"Expected 'entering' log, got: {info_msgs}"
        )
        # "entering" should be the first log emitted
        first_call_msg = str(mock_logger.info.call_args_list[0])
        assert "entering" in first_call_msg

    def test_logs_no_state_dir(self, tmp_path):
        """Logs INFO when .egg-state directory does not exist."""
        from routes.pipelines import _commit_statefiles_to_worktree

        with patch("routes.pipelines.logger") as mock_logger:
            _commit_statefiles_to_worktree(tmp_path, "Test commit", pipeline_identifier=42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("no .egg-state directory" in msg for msg in info_msgs), (
            f"Expected 'no .egg-state directory' log, got: {info_msgs}"
        )

    def test_logs_glob_match_results(self, tmp_path):
        """Glob match count and paths are logged at INFO when pipeline_identifier is set."""
        from routes.pipelines import _commit_statefiles_to_worktree

        # Create matching state files
        state_dir = tmp_path / ".egg-state" / "contracts"
        state_dir.mkdir(parents=True)
        (state_dir / "42.json").write_text("{}", encoding="utf-8")
        (state_dir / "42-plan.md").write_text("plan", encoding="utf-8")

        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _commit_statefiles_to_worktree(tmp_path, "Test commit", pipeline_identifier=42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("glob match results" in msg for msg in info_msgs), (
            f"Expected glob match log, got: {info_msgs}"
        )
        glob_call = [c for c in mock_logger.info.call_args_list if "glob match" in str(c)]
        assert glob_call[0][1].get("match_count") >= 1
        assert "matched_paths" in glob_call[0][1]

    def test_logs_nothing_staged(self, tmp_path):
        """Logs INFO when nothing is staged (returncode 0 from git diff --cached)."""
        from routes.pipelines import _commit_statefiles_to_worktree

        state_dir = tmp_path / ".egg-state"
        state_dir.mkdir(parents=True)
        (state_dir / "42.json").write_text("{}", encoding="utf-8")

        with (
            patch("routes.pipelines.subprocess.run") as mock_run,
            patch("routes.pipelines.logger") as mock_logger,
        ):
            # git add succeeds, git diff --cached returns 0 (no changes)
            mock_run.return_value = MagicMock(returncode=0)
            _commit_statefiles_to_worktree(tmp_path, "Test commit", pipeline_identifier=42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("nothing staged" in msg for msg in info_msgs), (
            f"Expected 'nothing staged' log, got: {info_msgs}"
        )

    def test_logs_staged_changes_and_commit_success(self, tmp_path):
        """Logs INFO for staged changes and commit success."""
        from routes.pipelines import _commit_statefiles_to_worktree

        state_dir = tmp_path / ".egg-state"
        state_dir.mkdir(parents=True)
        (state_dir / "42.json").write_text("{}", encoding="utf-8")

        call_count = {"n": 0}

        def fake_run(cmd, **kwargs):
            call_count["n"] += 1
            result = MagicMock()
            if "diff" in cmd:
                result.returncode = 1  # changes staged
            else:
                result.returncode = 0
            return result

        with (
            patch("routes.pipelines.subprocess.run", side_effect=fake_run),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _commit_statefiles_to_worktree(tmp_path, "Test commit", pipeline_identifier=42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("staged changes detected" in msg for msg in info_msgs), (
            f"Expected 'staged changes detected' log, got: {info_msgs}"
        )
        assert any("commit succeeded" in msg for msg in info_msgs), (
            f"Expected 'commit succeeded' log, got: {info_msgs}"
        )


# ---------------------------------------------------------------------------
# _rewrite_brc_history_for_pr logging tests
# ---------------------------------------------------------------------------
class TestRewriteBrcHistoryForPrLogging:
    """Verify INFO-level diagnostic logging in _rewrite_brc_history_for_pr (#1633 task-1-3)."""

    def test_logs_entry_with_completed_phase_count(self, tmp_path):
        """Entry log includes total_phases and completed_phase_count."""
        from routes.pipelines import _rewrite_brc_history_for_pr

        phases = {
            "refine": MagicMock(status=PipelineStatus.COMPLETE),
            "plan": MagicMock(status=PipelineStatus.COMPLETE),
            "implement": MagicMock(status=PipelineStatus.FAILED),
        }

        with (
            patch("routes.pipelines._write_brc_history"),
            patch("routes.pipelines._commit_statefiles_to_worktree"),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _rewrite_brc_history_for_pr(tmp_path, "issue-42", phases, 42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("entering" in msg for msg in info_msgs)
        entry_call = [c for c in mock_logger.info.call_args_list if "entering" in str(c)]
        kwargs = entry_call[0][1]
        assert kwargs.get("total_phases") == 3
        assert kwargs.get("completed_phase_count") == 2
        assert set(kwargs.get("completed_phases", [])) == {"refine", "plan"}

    def test_logs_commit_success(self, tmp_path):
        """Logs INFO when commit step succeeds."""
        from routes.pipelines import _rewrite_brc_history_for_pr

        phases = {
            "implement": MagicMock(status=PipelineStatus.COMPLETE),
        }

        with (
            patch("routes.pipelines._write_brc_history"),
            patch("routes.pipelines._commit_statefiles_to_worktree"),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _rewrite_brc_history_for_pr(tmp_path, "issue-42", phases, 42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("commit step completed successfully" in msg for msg in info_msgs)

    def test_logs_exit(self, tmp_path):
        """Exit log is always emitted."""
        from routes.pipelines import _rewrite_brc_history_for_pr

        phases = {}

        with (
            patch("routes.pipelines._write_brc_history"),
            patch("routes.pipelines._commit_statefiles_to_worktree"),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _rewrite_brc_history_for_pr(tmp_path, "issue-42", phases, 42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("exiting" in msg for msg in info_msgs)

    def test_logs_exit_even_on_commit_failure(self, tmp_path):
        """Exit log is emitted even when commit fails."""
        from routes.pipelines import _rewrite_brc_history_for_pr

        phases = {
            "implement": MagicMock(status=PipelineStatus.COMPLETE),
        }

        with (
            patch("routes.pipelines._write_brc_history"),
            patch(
                "routes.pipelines._commit_statefiles_to_worktree",
                side_effect=subprocess.CalledProcessError(1, "git commit"),
            ),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _rewrite_brc_history_for_pr(tmp_path, "issue-42", phases, 42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("exiting" in msg for msg in info_msgs), (
            "Exit log should be emitted even when commit fails"
        )


# ---------------------------------------------------------------------------
# Integration test: real git repo with BRC history (task-1-5)
# ---------------------------------------------------------------------------
class TestIntegrationBrcHistory:
    """Integration test for the full _rewrite_brc_history_for_pr call chain (#1633 task-1-5).

    Exercises real function calls (not mocked away) with BRC messages in a
    mock store and draft files on disk.  Git operations are intercepted via
    monkeypatch since git init is unavailable in the test sandbox.

    Verifies:
      (a) _write_brc_history is called for each COMPLETE phase (not mocked)
      (b) BRC history files are written to disk with correct content
      (c) _commit_statefiles_to_worktree is called with "Persist BRC history" message
      (d) Draft files are preserved on disk (no cleanup — see #1713)
    """

    @pytest.fixture()
    def worktree(self, tmp_path):
        """Create a temporary directory structure mimicking a worktree."""
        repo = tmp_path / "repo"
        repo.mkdir()

        # Create draft files
        drafts_dir = repo / ".egg-state" / "drafts"
        drafts_dir.mkdir(parents=True)
        (drafts_dir / "42-analysis.md").write_text("Draft analysis", encoding="utf-8")
        (drafts_dir / "42-plan.md").write_text("Draft plan", encoding="utf-8")
        (drafts_dir / "99-analysis.md").write_text("Other pipeline draft", encoding="utf-8")

        # Create brc-history dir
        brc_dir = repo / ".egg-state" / "brc-history"
        brc_dir.mkdir(parents=True)

        # Create contract file
        contracts_dir = repo / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True)
        (contracts_dir / "42.json").write_text("{}", encoding="utf-8")

        return repo

    def test_full_chain_writes_brc_files_and_preserves_drafts(self, worktree, monkeypatch):
        """Full integration: rewrite writes BRC history files and drafts remain on disk."""
        import routes.pipelines as mod

        pipeline_id = "issue-42"
        identifier = 42

        # Create BRC messages for two completed phases
        brc_messages = [
            _make_brc_message(
                pipeline_id=pipeline_id,
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Proposal",
                body="Implementing feature",
                phase="refine",
            ),
            _make_brc_message(
                pipeline_id=pipeline_id,
                from_role="reviewer_code",
                message_type=MessageType.CONSENSUS_ACK,
                subject="ACK",
                body="Looks good",
                phase="refine",
            ),
            _make_brc_message(
                pipeline_id=pipeline_id,
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Implementation proposal",
                body="Code changes",
                phase="implement",
            ),
            _make_brc_message(
                pipeline_id=pipeline_id,
                from_role="tester",
                message_type=MessageType.CONSENSUS_ACK,
                subject="Tests pass",
                body="All tests pass",
                phase="implement",
            ),
        ]

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = brc_messages

        phases = {
            "refine": MagicMock(status=PipelineStatus.COMPLETE),
            "implement": MagicMock(status=PipelineStatus.COMPLETE),
        }

        # Track git commands issued
        git_commands: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            git_commands.append(cmd)
            result = MagicMock()
            # git diff --cached --quiet returns 1 when changes are staged
            if "diff" in cmd and "--cached" in cmd:
                result.returncode = 1
            else:
                result.returncode = 0
            # subprocess.run returns bytes unless text=True is passed; the
            # ls-files probe in _restore_missing_state_files_from_head omits
            # text= and splits stdout on b"\0", so the mock must match.
            if kwargs.get("text"):
                result.stdout = ""
                result.stderr = ""
            else:
                result.stdout = b""
                result.stderr = b""
            return result

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        # Step 1: Rewrite BRC history — calls _write_brc_history for each phase
        with patch("message_store.get_message_store", return_value=mock_store):
            mod._rewrite_brc_history_for_pr(worktree, pipeline_id, phases, identifier)

        # Assertion (a): BRC history files written for refine on the
        # work worktree. Implement phase: NO per-slice files on work
        # (#2755 — they're owned by each slice's integration branch
        # and committed there by the slice hook; duplicating onto
        # ``work`` would conflict with slice PR merges).
        brc_dir = worktree / ".egg-state" / "brc-history"
        assert (brc_dir / "42-refine.md").exists(), "BRC history for refine should exist"
        impl_file = brc_dir / f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md"
        assert not impl_file.exists(), (
            "Per-slice implement file leaked onto work worktree (#2755 regression)"
        )
        assert not (brc_dir / "42-implement.md").exists(), (
            "Aggregate implement.md leaked through hard switchover"
        )

        # Assertion (b): BRC files have correct content
        refine_content = (brc_dir / "42-refine.md").read_text()
        assert "# BRC Consensus History" in refine_content
        assert "refine phase" in refine_content
        assert "Pipeline: issue-42" in refine_content
        assert "CONSENSUS_PROPOSE" in refine_content
        assert "CONSENSUS_ACK" in refine_content

        # Assertion (c): _commit_statefiles_to_worktree was called (git commands issued)
        commit_cmds = [c for c in git_commands if "commit" in c]
        assert len(commit_cmds) >= 1, "Expected at least one git commit command"
        persist_commit = [c for c in commit_cmds if any("Persist BRC history" in arg for arg in c)]
        assert len(persist_commit) >= 1, (
            f"Expected commit with 'Persist BRC history' message, got: {commit_cmds}"
        )

        # Assertion (d): draft files are preserved — the PR phase no longer
        # removes them (see #1713). All three drafts (this pipeline's and
        # another pipeline's) must still be on disk.
        drafts_dir = worktree / ".egg-state" / "drafts"
        assert (drafts_dir / "42-analysis.md").exists(), (
            "Pipeline 42 analysis draft should be preserved"
        )
        assert (drafts_dir / "42-plan.md").exists(), "Pipeline 42 plan draft should be preserved"
        assert (drafts_dir / "99-analysis.md").exists(), "Pipeline 99's draft should be preserved"

    def test_brc_history_file_content(self, worktree, monkeypatch):
        """BRC history files contain expected markdown content.

        Uses the refine phase because implement-phase BRC history no
        longer lands on the work worktree (#2755) — per-slice files
        are owned by each slice's integration branch. Refine still
        writes an aggregate file to ``work``, so it's the right
        surface for verifying the writer's markdown rendering on
        the work-worktree path.
        """
        import routes.pipelines as mod

        messages = [
            _make_brc_message(
                pipeline_id="issue-42",
                from_role="coder",
                message_type=MessageType.CONSENSUS_PROPOSE,
                subject="Feature proposal",
                body="Implementation details",
                phase="refine",
            ),
        ]

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        phases = {
            "refine": MagicMock(status=PipelineStatus.COMPLETE),
        }

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        with patch("message_store.get_message_store", return_value=mock_store):
            mod._rewrite_brc_history_for_pr(worktree, "issue-42", phases, 42)

        history_file = worktree / ".egg-state" / "brc-history" / "42-refine.md"
        assert history_file.exists(), "BRC history file should exist on disk"

        content = history_file.read_text()
        assert "# BRC Consensus History" in content
        assert "refine phase" in content
        assert "Pipeline: issue-42" in content
        assert "coder" in content
        assert "CONSENSUS_PROPOSE" in content
        assert "Feature proposal" in content
        assert "Implementation details" in content

    def test_rewrite_noop_when_no_brc_messages(self, worktree, monkeypatch):
        """No BRC history files created when message store returns empty list."""
        import routes.pipelines as mod

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = []

        phases = {
            "implement": MagicMock(status=PipelineStatus.COMPLETE),
        }

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        with patch("message_store.get_message_store", return_value=mock_store):
            mod._rewrite_brc_history_for_pr(worktree, "issue-42", phases, 42)

        brc_dir = worktree / ".egg-state" / "brc-history"
        brc_files = list(brc_dir.glob("42-*.md"))
        assert len(brc_files) == 0, "No BRC files should be created when no messages exist"

    def test_rewrite_only_processes_complete_phases(self, worktree, monkeypatch):
        """Only phases with COMPLETE status get BRC history written."""
        import routes.pipelines as mod

        messages = [
            _make_brc_message(phase="refine"),
            _make_brc_message(phase="plan"),
            _make_brc_message(phase="implement"),
        ]

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        phases = {
            "refine": MagicMock(status=PipelineStatus.COMPLETE),
            "plan": MagicMock(status=PipelineStatus.FAILED),
            "implement": MagicMock(status=PipelineStatus.RUNNING),
        }

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr(mod.subprocess, "run", fake_run)

        with patch("message_store.get_message_store", return_value=mock_store):
            mod._rewrite_brc_history_for_pr(worktree, "issue-42", phases, 42)

        brc_dir = worktree / ".egg-state" / "brc-history"
        assert (brc_dir / "42-refine.md").exists(), "COMPLETE phase should have BRC file"
        assert not (brc_dir / "42-plan.md").exists(), "FAILED phase should NOT have BRC file"
        # #2548: implement is per-slice now; no aggregate, and the per-slice
        # file should also be absent because the implement phase is RUNNING.
        assert not (brc_dir / "42-implement.md").exists(), "RUNNING phase should NOT have BRC file"
        assert not (brc_dir / f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md").exists(), (
            "RUNNING phase should NOT have per-slice BRC file"
        )


# ---------------------------------------------------------------------------
# Edge cases and boundary conditions
# ---------------------------------------------------------------------------
class TestEdgeCases:
    """Additional edge case tests for gap coverage."""

    def test_write_brc_history_empty_messages_early_return(self, tmp_path):
        """New early return for empty messages list doesn't write any file."""
        from routes.pipelines import _write_brc_history

        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = []

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)

        history_dir = tmp_path / ".egg-state" / "brc-history"
        if history_dir.exists():
            assert list(history_dir.iterdir()) == []

    def test_write_brc_history_messages_exist_but_wrong_phase(self, tmp_path):
        """Messages exist but none match the requested phase."""
        from routes.pipelines import _write_brc_history

        messages = [_make_brc_message(phase="plan")]  # Phase is "plan"
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", 42)  # Request "implement"

        history_dir = tmp_path / ".egg-state" / "brc-history"
        # #2548: neither aggregate nor per-slice file should exist when no
        # implement-phase messages are present.
        assert not (history_dir / "42-implement.md").exists()
        assert not (history_dir / f"42-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md").exists()

    def test_write_brc_history_string_identifier(self, tmp_path):
        """_write_brc_history works with string pipeline identifiers."""
        from routes.pipelines import _write_brc_history

        messages = [_make_brc_message(phase="implement")]
        mock_store = MagicMock(spec=MessageStore)
        mock_store.get_messages.return_value = messages

        with patch("message_store.get_message_store", return_value=mock_store):
            _write_brc_history(tmp_path, "issue-42", "implement", "my-pipeline")

        # #2548: implement is per-slice — the aggregate file is gone.
        history_file = (
            tmp_path
            / ".egg-state"
            / "brc-history"
            / f"my-pipeline-implement-{_DEFAULT_IMPLEMENT_SLICE_ID}.md"
        )
        assert history_file.exists()
        assert not (tmp_path / ".egg-state" / "brc-history" / "my-pipeline-implement.md").exists()

    def test_rewrite_brc_history_mixed_statuses_logging(self, tmp_path):
        """Entry log correctly reports completed vs non-completed phase counts."""
        from routes.pipelines import _rewrite_brc_history_for_pr

        phases = {
            "refine": MagicMock(status=PipelineStatus.COMPLETE),
            "plan": MagicMock(status=PipelineStatus.FAILED),
            "implement": MagicMock(status=PipelineStatus.RUNNING),
            "review": MagicMock(status=PipelineStatus.COMPLETE),
        }

        with (
            patch("routes.pipelines._write_brc_history"),
            patch("routes.pipelines._commit_statefiles_to_worktree"),
            patch("routes.pipelines.logger") as mock_logger,
        ):
            _rewrite_brc_history_for_pr(tmp_path, "issue-42", phases, 42)

        entry_call = [c for c in mock_logger.info.call_args_list if "entering" in str(c)]
        kwargs = entry_call[0][1]
        assert kwargs.get("total_phases") == 4
        assert kwargs.get("completed_phase_count") == 2

    def test_commit_statefiles_logs_entry_and_exit_when_state_dir_missing(self, tmp_path):
        """Entry and exit logs are emitted when .egg-state directory doesn't exist."""
        from routes.pipelines import _commit_statefiles_to_worktree

        with patch("routes.pipelines.logger") as mock_logger:
            _commit_statefiles_to_worktree(tmp_path, "Test commit", pipeline_identifier=42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("entering" in msg for msg in info_msgs)
        assert any("no .egg-state directory" in msg for msg in info_msgs)

    def test_commit_statefiles_no_match_returns_after_logging(self, tmp_path):
        """When glob finds no matches, the function logs and returns early."""
        from routes.pipelines import _commit_statefiles_to_worktree

        state_dir = tmp_path / ".egg-state"
        state_dir.mkdir(parents=True)
        # Create a file for a different pipeline
        (state_dir / "99.json").write_text("{}", encoding="utf-8")

        with patch("routes.pipelines.logger") as mock_logger:
            _commit_statefiles_to_worktree(tmp_path, "Test commit", pipeline_identifier=42)

        info_msgs = [str(c) for c in mock_logger.info.call_args_list]
        assert any("glob match" in msg for msg in info_msgs)
        glob_call = [c for c in mock_logger.info.call_args_list if "glob match" in str(c)]
        assert glob_call[0][1].get("match_count") == 0
