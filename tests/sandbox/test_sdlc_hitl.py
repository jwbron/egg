"""Tests for sandbox/egg_lib/sdlc_hitl.py - HITL checkpoint handling."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib.orch_client import OrchestratorError
from egg_lib.sdlc_hitl import (
    _detect_phase,
    _find_repo_path,
    _get_draft_path,
    _read_draft,
    handle_hitl_checkpoint,
)

# ---------------------------------------------------------------------------
# _detect_phase tests
# ---------------------------------------------------------------------------


class TestDetectPhase:
    """_detect_phase uses substring matching on the question text."""

    def test_string_context_ignored(self):
        """String context is accepted but detection uses the question."""
        assert _detect_phase("implement changes", "some string context") == "implement"

    def test_context_none(self):
        assert _detect_phase("review the pr", None) == "pr"

    def test_refine_keyword(self):
        assert _detect_phase("Please refine the draft") == "refine"

    def test_analysis_keyword(self):
        assert _detect_phase("Review the analysis document") == "refine"

    def test_plan_keyword(self):
        assert _detect_phase("Approve the plan") == "plan"

    def test_implement_keyword(self):
        assert _detect_phase("Ready to implement?") == "implement"

    def test_pr_keyword(self):
        assert _detect_phase("Review the PR changes") == "pr"

    def test_pr_word_boundary(self):
        """'pr' must match as a whole word, not as a substring of other words."""
        # "approve" contains "pr" but should NOT match as phase "pr"
        assert _detect_phase("approve this change") == "unknown"
        assert _detect_phase("improve the approach") == "unknown"
        assert _detect_phase("comprehensive review") == "unknown"

    def test_unknown_question(self):
        assert _detect_phase("Something else entirely") == "unknown"

    def test_case_insensitive(self):
        assert _detect_phase("REFINE the document") == "refine"
        assert _detect_phase("PLAN approval needed") == "plan"


# ---------------------------------------------------------------------------
# _get_draft_path tests
# ---------------------------------------------------------------------------


class TestGetDraftPath:
    def test_refine_issue_mode(self):
        path = _get_draft_path("refine", "issue", issue_number=42)
        assert path == ".egg-state/drafts/42-analysis.md"

    def test_refine_local_mode(self):
        path = _get_draft_path("refine", "local", pipeline_id="local-abc")
        assert path == ".egg-state/drafts/local-abc-analysis.md"

    def test_implement_returns_none(self):
        path = _get_draft_path("implement", "issue", issue_number=1)
        assert path is None

    def test_plan_phase(self):
        path = _get_draft_path("plan", "issue", issue_number=10)
        assert path == ".egg-state/drafts/10-plan.md"

    def test_pr_phase(self):
        path = _get_draft_path("pr", "issue", issue_number=5)
        assert path == ".egg-state/drafts/5-pr.md"

    def test_local_mode_fallback_prefix(self):
        """When no pipeline_id given in local mode, prefix is 'local'."""
        path = _get_draft_path("refine", "local")
        assert path == ".egg-state/drafts/local-analysis.md"


# ---------------------------------------------------------------------------
# _find_repo_path tests
# ---------------------------------------------------------------------------


class TestFindRepoPath:
    def test_git_rev_parse_success(self, tmp_path):
        """When git rev-parse succeeds, its output is used."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=str(tmp_path) + "\n",
            )
            result = _find_repo_path()
            assert result == tmp_path
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args == ["git", "rev-parse", "--show-toplevel"]

    def test_git_rev_parse_failure_falls_back(self, tmp_path, monkeypatch):
        """When git rev-parse fails, falls back to cwd traversal."""
        # Create a .git dir in tmp_path
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = _find_repo_path()
            assert result == tmp_path

    def test_no_git_anywhere(self, tmp_path, monkeypatch):
        """When no .git found, returns cwd."""
        monkeypatch.chdir(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = _find_repo_path()
            assert result == tmp_path

    def test_git_command_not_found(self, tmp_path, monkeypatch):
        """When git is not installed, falls back gracefully."""
        monkeypatch.chdir(tmp_path)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _find_repo_path()
            assert result == tmp_path


# ---------------------------------------------------------------------------
# _read_draft tests
# ---------------------------------------------------------------------------


class TestReadDraft:
    def test_read_existing_file(self, tmp_path):
        draft = tmp_path / "draft.md"
        draft.write_text("# Hello\nWorld")
        assert _read_draft(tmp_path, "draft.md") == "# Hello\nWorld"

    def test_none_rel_path(self, tmp_path):
        assert _read_draft(tmp_path, None) is None

    def test_missing_file(self, tmp_path):
        assert _read_draft(tmp_path, "missing.md") is None


# ---------------------------------------------------------------------------
# handle_hitl_checkpoint tests
# ---------------------------------------------------------------------------


class TestHandleHitlCheckpoint:
    """Tests for the interactive HITL checkpoint handler.

    We mock stdin (user input), stdout (display), and the OrchClient methods.
    """

    def _make_decision(self, **overrides):
        base = {
            "id": "d1",
            "question": "Approve the refine analysis?",
            "context": "Draft content for refine phase",
        }
        base.update(overrides)
        return base

    def _make_client(self, resolve_return=None, cancel_return=None):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = resolve_return or {"status": "resolved"}
        client.cancel_pipeline.return_value = cancel_return or {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_approve(self, mock_input, mock_repo, tmp_path, capsys):
        """User selects option 3 to approve."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"

        client = self._make_client()
        decision = self._make_decision()

        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        client.resolve_decision.assert_called_once_with("issue-42", "d1", "Approved")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_cancel(self, mock_input, mock_repo, tmp_path, capsys):
        """User selects option 5 to cancel."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "5"

        client = self._make_client()
        decision = self._make_decision()

        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "cancelled"
        client.cancel_pipeline.assert_called_once_with("issue-42")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_feedback_then_resolve(self, mock_input, mock_repo, tmp_path, capsys):
        """User selects option 4, provides feedback, then it resolves."""
        mock_repo.return_value = tmp_path
        # First call: menu choice "4", then feedback lines, then empty to finish
        mock_input.side_effect = ["4", "Please add more detail", ""]

        client = self._make_client()
        decision = self._make_decision()

        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        client.resolve_decision.assert_called_once_with("issue-42", "d1", "Please add more detail")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_feedback_empty_returns_to_menu(self, mock_input, mock_repo, tmp_path, capsys):
        """Empty feedback returns to menu; user then approves."""
        mock_repo.return_value = tmp_path
        # choice 4 → empty feedback → back to menu → choice 3 (approve)
        mock_input.side_effect = ["4", "", "3"]

        client = self._make_client()
        decision = self._make_decision()

        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        client.resolve_decision.assert_called_once_with("issue-42", "d1", "Approved")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_eof_cancels(self, mock_input, mock_repo, tmp_path, capsys):
        """EOFError during input defaults to cancel (option 5)."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = EOFError

        client = self._make_client()
        decision = self._make_decision()

        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "cancelled"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_edit_no_draft_returns_to_menu(self, mock_input, mock_repo, tmp_path, capsys):
        """Option 1 (edit) with no draft file returns to menu."""
        mock_repo.return_value = tmp_path
        # choice 1 → no draft → back to menu → choice 3 (approve)
        mock_input.side_effect = ["1", "3"]

        client = self._make_client()
        decision = self._make_decision(
            question="Ready to implement?",
            context="",
        )  # implement phase has no draft

        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._launch_editor")
    @patch("builtins.input")
    def test_edit_with_draft(self, mock_input, mock_editor, mock_repo, tmp_path, capsys):
        """Option 1 (edit) launches editor on existing draft file."""
        # Create the draft file
        draft_dir = tmp_path / ".egg-state" / "drafts"
        draft_dir.mkdir(parents=True)
        draft_file = draft_dir / "42-analysis.md"
        draft_file.write_text("# Analysis\nDraft content")

        mock_repo.return_value = tmp_path
        mock_editor.return_value = True
        # choice 1 → editor opens → back to menu → choice 3 (approve)
        mock_input.side_effect = ["1", "3"]

        client = self._make_client()
        decision = self._make_decision()

        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        mock_editor.assert_called_once_with(draft_file)

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_invalid_choice_retries(self, mock_input, mock_repo, tmp_path, capsys):
        """Invalid menu choices prompt again."""
        mock_repo.return_value = tmp_path
        # "x" is invalid → retries → then "3" to approve
        mock_input.side_effect = ["x", "3"]

        client = self._make_client()
        decision = self._make_decision()

        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_resolve_failure_retries(self, mock_input, mock_repo, tmp_path, capsys):
        """If resolve_decision fails, user is returned to menu."""
        mock_repo.return_value = tmp_path
        # First "3" → API fails → back to menu → "3" again → succeeds
        mock_input.side_effect = ["3", "3"]

        client = self._make_client()
        client.resolve_decision.side_effect = [
            OrchestratorError("server error", status_code=500),
            {"status": "resolved"},
        ]
        decision = self._make_decision()

        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        assert client.resolve_decision.call_count == 2

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_draft_preview_shown(self, mock_input, mock_repo, tmp_path, capsys):
        """When a draft exists, its preview is displayed."""
        draft_dir = tmp_path / ".egg-state" / "drafts"
        draft_dir.mkdir(parents=True)
        (draft_dir / "42-analysis.md").write_text("# Analysis\nContent here")

        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"

        client = self._make_client()
        decision = self._make_decision()

        handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        captured = capsys.readouterr()
        assert "Draft Preview" in captured.out
        assert "Analysis" in captured.out

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._launch_claude")
    @patch("builtins.input")
    def test_launch_claude(self, mock_input, mock_claude, mock_repo, tmp_path, capsys):
        """Option 2 launches Claude session, then returns to menu."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["2", "3"]

        client = self._make_client()
        decision = self._make_decision()

        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        mock_claude.assert_called_once_with(tmp_path)
