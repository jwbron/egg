"""Tests for sandbox/egg_lib/sdlc_hitl.py - HITL checkpoint handling."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib.orch_client import OrchestratorError
from egg_lib.sdlc_hitl import (
    _detect_phase,
    _display_in_pager,
    _find_repo_path,
    _get_contract_key,
    _get_draft_path,
    _handle_contract_questions,
    _launch_claude,
    _load_pending_contract_decisions,
    _read_draft,
    _resolve_contract_decision,
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

    def test_plan_word_boundary(self):
        """'plan' must match as a whole word, not as a substring of other words."""
        assert _detect_phase("explanation of changes") == "unknown"
        assert _detect_phase("unplanned outage occurred") == "unknown"

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
    def test_egg_repos_env_single_repo(self, tmp_path, monkeypatch):
        """When EGG_REPOS has a single repo, use its directory under ~/repos/."""
        repos_dir = tmp_path / "repos"
        repo_dir = repos_dir / "myrepo"
        repo_dir.mkdir(parents=True)
        monkeypatch.setenv("EGG_REPOS", "owner/myrepo")
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _find_repo_path()
        assert result == repo_dir

    def test_egg_repos_env_multiple_repos_falls_through(self, tmp_path, monkeypatch):
        """When EGG_REPOS has multiple repos, fall through to other strategies."""
        repos_dir = tmp_path / "repos"
        (repos_dir / "a").mkdir(parents=True)
        (repos_dir / "b").mkdir(parents=True)
        monkeypatch.setenv("EGG_REPOS", "owner/a,owner/b")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = _find_repo_path()
            # Falls through to cwd since git also fails
            assert result == tmp_path

    def test_single_repo_under_repos_dir(self, tmp_path, monkeypatch):
        """When ~/repos/ has exactly one subdirectory, use it."""
        repos_dir = tmp_path / "repos"
        repo_dir = repos_dir / "only-repo"
        repo_dir.mkdir(parents=True)
        monkeypatch.delenv("EGG_REPOS", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _find_repo_path()
        assert result == repo_dir

    def test_git_rev_parse_success(self, tmp_path, monkeypatch):
        """When git rev-parse succeeds, its output is used."""
        monkeypatch.delenv("EGG_REPOS", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=str(tmp_path) + "\n",
            )
            result = _find_repo_path()
            assert result == tmp_path

    def test_git_rev_parse_failure_falls_back(self, tmp_path, monkeypatch):
        """When git rev-parse fails, falls back to cwd traversal."""
        # Create a .git dir in tmp_path
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("EGG_REPOS", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = _find_repo_path()
            assert result == tmp_path

    def test_no_git_anywhere(self, tmp_path, monkeypatch):
        """When no .git found, returns cwd."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("EGG_REPOS", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = _find_repo_path()
            assert result == tmp_path

    def test_git_command_not_found(self, tmp_path, monkeypatch):
        """When git is not installed, falls back gracefully."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("EGG_REPOS", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
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
        """EOFError during input defaults to cancel via universal 'c' option.

        _prompt_choice returns 'c' on EOF/interrupt.  We route through a
        type-aware handler (choice with options) where 'c' is in the valid
        set so it triggers _handle_universal_option → cancel.
        """
        mock_repo.return_value = tmp_path
        mock_input.side_effect = EOFError

        client = self._make_client()
        decision = self._make_decision(
            decision_type="choice",
            options=["Option A", "Option B"],
        )

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
    @patch("egg_lib.sdlc_hitl._launch_editor")
    @patch("builtins.input")
    def test_edit_creates_missing_draft(self, mock_input, mock_editor, mock_repo, tmp_path, capsys):
        """Option 1 (edit) creates the draft file from decision context when file doesn't exist."""
        mock_repo.return_value = tmp_path
        mock_editor.return_value = True
        # choice 1 → draft created & editor opens → back to menu → choice 3 (approve)
        mock_input.side_effect = ["1", "3"]

        client = self._make_client()
        decision = self._make_decision()  # "refine" phase, has context

        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        draft_file = tmp_path / ".egg-state" / "drafts" / "42-analysis.md"
        assert draft_file.exists()
        # File should contain the decision context (fallback from worktree)
        assert draft_file.read_text() == "Draft content for refine phase"
        mock_editor.assert_called_once_with(draft_file)

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._launch_editor")
    @patch("builtins.input")
    def test_edit_creates_stub_when_no_context(
        self, mock_input, mock_editor, mock_repo, tmp_path, capsys
    ):
        """Option 1 (edit) creates a stub file when no draft or context exists."""
        mock_repo.return_value = tmp_path
        mock_editor.return_value = True
        mock_input.side_effect = ["1", "3"]

        client = self._make_client()
        decision = self._make_decision(context="")

        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        draft_file = tmp_path / ".egg-state" / "drafts" / "42-analysis.md"
        assert draft_file.exists()
        assert draft_file.read_text() == "# Draft: refine\n\n"
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
        mock_claude.assert_called_once_with(
            tmp_path,
            ".egg-state/drafts/42-analysis.md",
            "refine",
            42,
        )


# ---------------------------------------------------------------------------
# Phase field in decision dict tests
# ---------------------------------------------------------------------------


class TestDecisionPhaseField:
    """Tests for explicit 'phase' field in decision dict vs _detect_phase fallback."""

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._get_draft_path")
    @patch("builtins.input")
    def test_explicit_phase_overrides_detect(
        self, mock_input, mock_draft_path, mock_repo, tmp_path
    ):
        """When decision dict has an explicit 'phase', it is used instead of regex detection."""
        mock_repo.return_value = tmp_path
        mock_draft_path.return_value = None
        mock_input.return_value = "3"  # approve

        client = self._make_client()
        # Question text says "refine" but explicit phase is "plan"
        decision = {
            "id": "d1",
            "question": "Approve the refine analysis?",
            "context": "",
            "phase": "plan",
        }

        handle_hitl_checkpoint(client, "issue-1", decision, pipeline_mode="issue", issue_number=1)

        # _get_draft_path should have been called with "plan", not "refine"
        mock_draft_path.assert_called_once_with("plan", "issue", 1, "issue-1")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._get_draft_path")
    @patch("builtins.input")
    def test_missing_phase_falls_back_to_detect(
        self, mock_input, mock_draft_path, mock_repo, tmp_path
    ):
        """When decision dict has no 'phase' key, _detect_phase is used."""
        mock_repo.return_value = tmp_path
        mock_draft_path.return_value = None
        mock_input.return_value = "3"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the refine analysis?",
            "context": "",
        }

        handle_hitl_checkpoint(client, "issue-1", decision, pipeline_mode="issue", issue_number=1)

        # _detect_phase should resolve "refine" from the question text
        mock_draft_path.assert_called_once_with("refine", "issue", 1, "issue-1")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._get_draft_path")
    @patch("builtins.input")
    def test_none_phase_falls_back_to_detect(
        self, mock_input, mock_draft_path, mock_repo, tmp_path
    ):
        """When decision dict has phase=None, _detect_phase is used as fallback."""
        mock_repo.return_value = tmp_path
        mock_draft_path.return_value = None
        mock_input.return_value = "3"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Ready to implement?",
            "context": "",
            "phase": None,
        }

        handle_hitl_checkpoint(client, "issue-1", decision, pipeline_mode="issue", issue_number=1)

        # _detect_phase should resolve "implement" from the question text
        mock_draft_path.assert_called_once_with("implement", "issue", 1, "issue-1")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._get_draft_path")
    @patch("builtins.input")
    def test_unknown_phase_fetches_from_pipeline_api(
        self, mock_input, mock_draft_path, mock_repo, tmp_path
    ):
        """When _detect_phase returns 'unknown', fall back to pipeline API."""
        mock_repo.return_value = tmp_path
        mock_draft_path.return_value = None
        mock_input.return_value = "3"

        client = MagicMock(spec=["resolve_decision", "cancel_pipeline", "get_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.get_pipeline.return_value = {"pipeline": {"current_phase": "plan"}}

        # Question has no phase keywords → _detect_phase returns "unknown"
        decision = {
            "id": "d1",
            "question": "Something unrelated to any phase keyword",
            "context": "",
        }

        handle_hitl_checkpoint(client, "issue-1", decision, pipeline_mode="issue", issue_number=1)

        client.get_pipeline.assert_called_once_with("issue-1")
        mock_draft_path.assert_called_once_with("plan", "issue", 1, "issue-1")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._get_draft_path")
    @patch("builtins.input")
    def test_unknown_phase_api_failure_stays_unknown(
        self, mock_input, mock_draft_path, mock_repo, tmp_path
    ):
        """When API fetch fails, phase remains 'unknown' (logged, not raised)."""
        mock_repo.return_value = tmp_path
        mock_draft_path.return_value = None
        mock_input.return_value = "3"

        client = MagicMock(spec=["resolve_decision", "cancel_pipeline", "get_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.get_pipeline.side_effect = OrchestratorError("Connection refused")

        decision = {
            "id": "d1",
            "question": "Something unrelated to any phase keyword",
            "context": "",
        }

        handle_hitl_checkpoint(client, "issue-1", decision, pipeline_mode="issue", issue_number=1)

        client.get_pipeline.assert_called_once_with("issue-1")
        # Phase stays "unknown" since API failed
        mock_draft_path.assert_called_once_with("unknown", "issue", 1, "issue-1")


# ---------------------------------------------------------------------------
# _launch_claude unit tests
# ---------------------------------------------------------------------------


class TestLaunchClaude:
    """Unit tests for _launch_claude command construction."""

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    @patch("llm.runner.shutil.which", return_value="/usr/bin/claude")
    def test_with_draft_context(self, mock_which, mock_run, tmp_path):
        """When draft_rel is provided, --append-system-prompt includes rules and context."""
        _launch_claude(tmp_path, draft_rel="drafts/42-analysis.md", phase="refine", issue_number=42)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/claude"
        assert "--dangerously-skip-permissions" in cmd
        assert "--append-system-prompt" in cmd
        prompt_idx = cmd.index("--append-system-prompt")
        prompt_text = cmd[prompt_idx + 1]
        # Rules text should be included
        assert "HITL Draft Editing Session" in prompt_text
        # Context-specific parts
        assert "refine" in prompt_text
        assert "#42" in prompt_text
        assert "drafts/42-analysis.md" in prompt_text

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    @patch("llm.runner.shutil.which", return_value="/usr/bin/claude")
    def test_without_draft_context(self, mock_which, mock_run, tmp_path):
        """When draft_rel is None, command still includes rules and phase/issue context."""
        _launch_claude(tmp_path, draft_rel=None, phase="implement", issue_number=10)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/claude"
        assert "--dangerously-skip-permissions" in cmd
        # Rules are still injected even without a draft
        assert "--append-system-prompt" in cmd
        prompt_idx = cmd.index("--append-system-prompt")
        prompt_text = cmd[prompt_idx + 1]
        assert "HITL Draft Editing Session" in prompt_text
        # Phase and issue context are injected regardless of draft_rel
        assert "implement" in prompt_text
        assert "#10" in prompt_text
        # Draft-specific text should NOT appear
        assert "Draft file:" not in prompt_text
        assert mock_run.call_args[1]["cwd"] == str(tmp_path)

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    @patch("llm.runner.shutil.which", return_value="/usr/bin/claude")
    def test_with_draft_but_no_phase_or_issue(self, mock_which, mock_run, tmp_path):
        """When draft_rel is set but phase/issue are None, prompt still includes draft."""
        _launch_claude(tmp_path, draft_rel="drafts/1-plan.md", phase=None, issue_number=None)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--append-system-prompt" in cmd
        prompt_idx = cmd.index("--append-system-prompt")
        prompt_text = cmd[prompt_idx + 1]
        assert "drafts/1-plan.md" in prompt_text
        # Phase and issue should not appear in the prompt
        assert "Current phase:" not in prompt_text
        assert "Issue: #" not in prompt_text

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    @patch("llm.runner.shutil.which", return_value=None)
    def test_claude_not_found(self, mock_which, mock_run, tmp_path, capsys):
        """When claude binary is not found, prints error and returns."""
        _launch_claude(tmp_path, draft_rel="drafts/1-plan.md")
        mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "Claude CLI not found" in captured.out


# ---------------------------------------------------------------------------
# Type-aware terminal handler tests
# ---------------------------------------------------------------------------


class TestHandlePhaseGate:
    """Tests for phase_gate decision type rendering."""

    def _make_decision(self, **overrides):
        base = {
            "id": "d1",
            "question": "The refine phase has completed. Please review the analysis.",
            "context": "Draft content here",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        base.update(overrides)
        return base

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.cancel_pipeline.return_value = {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_approve_sends_json(self, mock_input, mock_repo, tmp_path, capsys):
        """Phase gate approve sends JSON {"action": "approve"}."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"

        client = self._make_client()
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            self._make_decision(),
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "approve"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_request_changes_sends_json(self, mock_input, mock_repo, tmp_path, capsys):
        """Phase gate request changes sends JSON with feedback."""
        mock_repo.return_value = tmp_path
        # "4" = request changes, then feedback text, then empty line to finish
        mock_input.side_effect = ["4", "Fix the error handling", ""]

        client = self._make_client()
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            self._make_decision(),
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "request_changes"
        assert resolution["feedback"] == "Fix the error handling"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._display_in_pager")
    @patch("builtins.input")
    def test_shows_document_in_pager(self, mock_input, mock_pager, mock_repo, tmp_path):
        """Phase gate shows full document in pager."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"

        client = self._make_client()
        handle_hitl_checkpoint(
            client,
            "issue-42",
            self._make_decision(),
            pipeline_mode="issue",
            issue_number=42,
        )

        mock_pager.assert_called_once_with("Draft content here")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_confirmation_displayed(self, mock_input, mock_repo, tmp_path, capsys):
        """Phase gate shows confirmation after approve."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"

        client = self._make_client()
        handle_hitl_checkpoint(
            client,
            "issue-42",
            self._make_decision(),
            pipeline_mode="issue",
            issue_number=42,
        )

        captured = capsys.readouterr()
        assert "Approved" in captured.out


class TestHandleChoice:
    """Tests for choice decision type rendering."""

    def _make_decision(self, **overrides):
        base = {
            "id": "d1",
            "question": "Which database should we use?",
            "context": "",
            "decision_type": "choice",
            "options": ["PostgreSQL", "MongoDB", "SQLite"],
        }
        base.update(overrides)
        return base

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.cancel_pipeline.return_value = {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_renders_options_as_numbered_list(self, mock_input, mock_repo, tmp_path, capsys):
        """Choice type renders options as numbered list."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "1"

        client = self._make_client()
        handle_hitl_checkpoint(
            client,
            "issue-42",
            self._make_decision(),
            pipeline_mode="issue",
            issue_number=42,
        )

        captured = capsys.readouterr()
        assert "PostgreSQL" in captured.out
        assert "MongoDB" in captured.out
        assert "SQLite" in captured.out

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_selection_sends_json(self, mock_input, mock_repo, tmp_path, capsys):
        """Choice selection sends JSON {"action": "select", "selected": "..."}."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "2"

        client = self._make_client()
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            self._make_decision(),
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "select"
        assert resolution["selected"] == "MongoDB"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_confirmation_shows_selected(self, mock_input, mock_repo, tmp_path, capsys):
        """Choice shows confirmation with selected option."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "1"

        client = self._make_client()
        handle_hitl_checkpoint(
            client,
            "issue-42",
            self._make_decision(),
            pipeline_mode="issue",
            issue_number=42,
        )

        captured = capsys.readouterr()
        assert "Selected: PostgreSQL" in captured.out


class TestHandleFeedback:
    """Tests for feedback decision type rendering."""

    def _make_decision(self, **overrides):
        base = {
            "id": "d1",
            "question": "Please provide feedback",
            "context": "",
            "decision_type": "feedback",
            "questions": [
                {"id": "q-1", "question": "What is expected volume?", "answer": ""},
                {"id": "q-2", "question": "Any performance reqs?", "answer": ""},
            ],
        }
        base.update(overrides)
        return base

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.cancel_pipeline.return_value = {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_prompts_each_question(self, mock_input, mock_repo, tmp_path, capsys):
        """Feedback type prompts each question individually."""
        mock_repo.return_value = tmp_path
        # Answer q1, answer q2, then 's' to submit
        mock_input.side_effect = ["High volume", "Under 100ms", "s"]

        client = self._make_client()
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            self._make_decision(),
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "submit_feedback"
        assert resolution["answers"]["q-1"] == "High volume"
        assert resolution["answers"]["q-2"] == "Under 100ms"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_redo_question(self, mock_input, mock_repo, tmp_path, capsys):
        """Feedback type supports redoing a question via [r]."""
        mock_repo.return_value = tmp_path
        # Answer q1, answer q2, 'r' to redo, pick question 1, new answer, 's' to submit
        mock_input.side_effect = ["First", "Second", "r", "1", "Updated first", "s"]

        client = self._make_client()
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            self._make_decision(),
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["answers"]["q-1"] == "Updated first"
        assert resolution["answers"]["q-2"] == "Second"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_empty_questions_falls_back_to_freetext(self, mock_input, mock_repo, tmp_path, capsys):
        """Feedback with empty questions falls back to single free-text input."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["My feedback here", ""]

        client = self._make_client()
        decision = self._make_decision(questions=[])
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "submit_feedback"
        assert "response" in resolution["answers"]

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_confirmation_shows_answer_count(self, mock_input, mock_repo, tmp_path, capsys):
        """Feedback shows confirmation with answer count."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["Answer 1", "Answer 2", "s"]

        client = self._make_client()
        handle_hitl_checkpoint(
            client,
            "issue-42",
            self._make_decision(),
            pipeline_mode="issue",
            issue_number=42,
        )

        captured = capsys.readouterr()
        assert "2 answer" in captured.out


class TestUniversalOptions:
    """Tests for universal options available on all decision types."""

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.cancel_pipeline.return_value = {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_cancel_on_phase_gate(self, mock_input, mock_repo, tmp_path):
        """Cancel option works on phase_gate."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "c"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the plan?",
            "context": "",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )
        assert result == "cancelled"
        client.cancel_pipeline.assert_called_once()

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_cancel_on_choice(self, mock_input, mock_repo, tmp_path):
        """Cancel option works on choice."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "c"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Pick one?",
            "context": "",
            "decision_type": "choice",
            "options": ["A", "B"],
        }
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
    def test_change_approach_sends_json(self, mock_input, mock_repo, tmp_path):
        """Change approach option sends JSON resolution."""
        mock_repo.return_value = tmp_path
        # 'a' = change approach, then feedback text, then empty line
        mock_input.side_effect = ["a", "Use a different architecture", ""]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the plan?",
            "context": "",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "change_approach"
        assert "different architecture" in resolution["feedback"]


class TestFallbackGenericMenu:
    """Tests for fallback to generic menu for unknown decision_type."""

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.cancel_pipeline.return_value = {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_unknown_type_falls_back(self, mock_input, mock_repo, tmp_path):
        """Unknown decision_type falls back to generic menu."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"  # Approve in generic menu

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the refine analysis?",
            "context": "Some context",
            "decision_type": "unknown_type",
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        # Generic menu uses bare "Approved" string
        client.resolve_decision.assert_called_once_with("issue-42", "d1", "Approved")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_choice_without_options_falls_back(self, mock_input, mock_repo, tmp_path):
        """Choice type with no options falls back to generic menu."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"  # Approve in generic menu

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the refine analysis?",
            "context": "Draft content here",
            "decision_type": "choice",
            "options": [],
        }
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
    def test_generic_cancel(self, mock_input, mock_repo, tmp_path):
        """Generic menu option 5 cancels the pipeline."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "5"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the refine analysis?",
            "context": "",
            "decision_type": "unknown_type",
        }
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
    def test_generic_eof_returns_cancelled(self, mock_input, mock_repo, tmp_path):
        """EOF on generic menu input returns 'c' which cancels the pipeline.

        When stdin is exhausted, _prompt_choice returns 'c'. The generic menu
        must handle 'c' as a cancel option (same as '5') rather than looping
        infinitely.
        """
        mock_repo.return_value = tmp_path
        mock_input.side_effect = EOFError()

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the refine analysis?",
            "context": "",
            "decision_type": "unknown_type",
        }
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
    def test_generic_keyboard_interrupt_returns_cancelled(self, mock_input, mock_repo, tmp_path):
        """KeyboardInterrupt on generic menu input cancels the pipeline."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = KeyboardInterrupt()

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the refine analysis?",
            "context": "",
            "decision_type": "unknown_type",
        }
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
    def test_generic_c_option_cancels(self, mock_input, mock_repo, tmp_path):
        """Explicit 'c' input on generic menu cancels the pipeline."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "c"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the refine analysis?",
            "context": "",
            "decision_type": "unknown_type",
        }
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
    def test_generic_feedback_then_approve(self, mock_input, mock_repo, tmp_path):
        """Generic menu option 4 collects feedback, then resolves."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["4", "Please improve formatting", "", "3"]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the refine analysis?",
            "context": "",
            "decision_type": "unknown_type",
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        # First call resolves with feedback text
        client.resolve_decision.assert_called_once_with(
            "issue-42", "d1", "Please improve formatting"
        )


# ---------------------------------------------------------------------------
# Edge cases for universal options
# ---------------------------------------------------------------------------


class TestUniversalOptionsEdgeCases:
    """Tests for edge cases in universal options across all decision types."""

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.cancel_pipeline.return_value = {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_general_feedback_empty_returns_to_menu(self, mock_input, mock_repo, tmp_path):
        """[f] with empty feedback returns to menu (no resolution sent)."""
        mock_repo.return_value = tmp_path
        # 'f' → empty feedback → back to menu → 'c' to cancel
        mock_input.side_effect = ["f", "", "c"]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the plan?",
            "context": "",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "cancelled"
        # resolve_decision should NOT have been called (empty feedback)
        client.resolve_decision.assert_not_called()

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_change_approach_empty_returns_to_menu(self, mock_input, mock_repo, tmp_path):
        """[a] with empty approach text returns to menu (no resolution sent)."""
        mock_repo.return_value = tmp_path
        # 'a' → empty text → back to menu → '3' approve
        mock_input.side_effect = ["a", "", "3"]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the plan?",
            "context": "",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        # Only the approve call should have been made
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "approve"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_general_feedback_with_text_on_choice(self, mock_input, mock_repo, tmp_path):
        """[f] with text on choice type resolves with approve + feedback."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["f", "I like option 1 but add caching", ""]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Which database?",
            "context": "",
            "decision_type": "choice",
            "options": ["PostgreSQL", "MongoDB"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "approve"
        assert "caching" in resolution["feedback"]

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_cancel_pipeline_api_failure(self, mock_input, mock_repo, tmp_path):
        """Cancel still returns 'cancelled' even when cancel_pipeline API fails."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "c"

        client = self._make_client()
        client.cancel_pipeline.side_effect = OrchestratorError("server error", status_code=500)
        decision = {
            "id": "d1",
            "question": "Pick?",
            "context": "",
            "decision_type": "choice",
            "options": ["A", "B"],
        }
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
    def test_change_approach_on_choice(self, mock_input, mock_repo, tmp_path):
        """[a] on choice type sends change_approach resolution."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["a", "Use DynamoDB instead", ""]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Which database?",
            "context": "",
            "decision_type": "choice",
            "options": ["PostgreSQL", "MongoDB"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "change_approach"
        assert "DynamoDB" in resolution["feedback"]


# ---------------------------------------------------------------------------
# Phase gate edge cases
# ---------------------------------------------------------------------------


class TestPhaseGateEdgeCases:
    """Tests for phase_gate decision type edge cases."""

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.cancel_pipeline.return_value = {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_request_changes_empty_feedback_loops(self, mock_input, mock_repo, tmp_path):
        """Phase gate option 4 with empty feedback loops back to menu."""
        mock_repo.return_value = tmp_path
        # '4' → empty feedback → back to menu → '3' approve
        mock_input.side_effect = ["4", "", "3"]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "The refine phase has completed.",
            "context": "Draft here",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        # Should be called once with approve (not with empty feedback)
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "approve"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_resolve_failure_retries_in_phase_gate(self, mock_input, mock_repo, tmp_path):
        """Phase gate approve fails, returns to menu, second approve succeeds."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["3", "3"]

        client = self._make_client()
        client.resolve_decision.side_effect = [
            OrchestratorError("server error", status_code=500),
            {"status": "resolved"},
        ]
        decision = {
            "id": "d1",
            "question": "The refine phase has completed.",
            "context": "",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
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
    def test_no_decision_id_uses_unknown(self, mock_input, mock_repo, tmp_path):
        """Decision dict with no 'id' field uses 'unknown' as decision ID."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"

        client = self._make_client()
        decision = {
            "question": "The refine phase has completed.",
            "context": "",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        assert call_args[0][1] == "unknown"  # decision_id

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_phase_gate_multiline_feedback(self, mock_input, mock_repo, tmp_path):
        """Phase gate request changes with multi-line feedback joins lines."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["4", "Line one", "Line two", ""]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "The refine phase has completed.",
            "context": "",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "request_changes"
        assert "Line one\nLine two" == resolution["feedback"]


# ---------------------------------------------------------------------------
# Feedback edge cases
# ---------------------------------------------------------------------------


class TestFeedbackEdgeCases:
    """Tests for feedback decision type edge cases."""

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.cancel_pipeline.return_value = {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_feedback_cancel_during_review(self, mock_input, mock_repo, tmp_path):
        """Feedback collected, then cancel during review loop."""
        mock_repo.return_value = tmp_path
        # Answer q1, answer q2, then 'c' to cancel in review loop
        mock_input.side_effect = ["Answer 1", "Answer 2", "c"]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Please provide feedback",
            "context": "",
            "decision_type": "feedback",
            "questions": [
                {"id": "q-1", "question": "Volume?", "answer": ""},
                {"id": "q-2", "question": "Performance?", "answer": ""},
            ],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "cancelled"
        client.cancel_pipeline.assert_called_once()

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_feedback_redo_invalid_question_number(self, mock_input, mock_repo, tmp_path):
        """Feedback redo with invalid question number shows error, stays in review loop."""
        mock_repo.return_value = tmp_path
        # Answer q1, answer q2, 'r' to redo, "99" invalid, then 's' to submit
        mock_input.side_effect = ["Answer 1", "Answer 2", "r", "99", "s"]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Please provide feedback",
            "context": "",
            "decision_type": "feedback",
            "questions": [
                {"id": "q-1", "question": "Volume?", "answer": ""},
                {"id": "q-2", "question": "Performance?", "answer": ""},
            ],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        # Original answers should remain (no change from invalid redo)
        assert resolution["answers"]["q-1"] == "Answer 1"
        assert resolution["answers"]["q-2"] == "Answer 2"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_feedback_redo_non_numeric(self, mock_input, mock_repo, tmp_path):
        """Feedback redo with non-numeric input shows error, stays in review loop."""
        mock_repo.return_value = tmp_path
        # Answer q1, answer q2, 'r', "abc" invalid, 's' to submit
        mock_input.side_effect = ["Answer 1", "Answer 2", "r", "abc", "s"]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Please provide feedback",
            "context": "",
            "decision_type": "feedback",
            "questions": [
                {"id": "q-1", "question": "Volume?", "answer": ""},
                {"id": "q-2", "question": "Performance?", "answer": ""},
            ],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["answers"]["q-1"] == "Answer 1"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_feedback_change_approach(self, mock_input, mock_repo, tmp_path):
        """Feedback type supports change approach from review loop."""
        mock_repo.return_value = tmp_path
        # Answer q1, answer q2, 'a' change approach, then text
        mock_input.side_effect = ["Answer 1", "Answer 2", "a", "Wrong approach entirely", ""]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Please provide feedback",
            "context": "",
            "decision_type": "feedback",
            "questions": [
                {"id": "q-1", "question": "Volume?", "answer": ""},
                {"id": "q-2", "question": "Performance?", "answer": ""},
            ],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "change_approach"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_feedback_resolve_failure_retries(self, mock_input, mock_repo, tmp_path):
        """Feedback submit fails, user stays in review loop and retries."""
        mock_repo.return_value = tmp_path
        # Answer q1, answer q2, 's' to submit (fails), 's' to retry (succeeds)
        mock_input.side_effect = ["Answer 1", "Answer 2", "s", "s"]

        client = self._make_client()
        client.resolve_decision.side_effect = [
            OrchestratorError("server error", status_code=500),
            {"status": "resolved"},
        ]
        decision = {
            "id": "d1",
            "question": "Please provide feedback",
            "context": "",
            "decision_type": "feedback",
            "questions": [
                {"id": "q-1", "question": "Volume?", "answer": ""},
                {"id": "q-2", "question": "Performance?", "answer": ""},
            ],
        }
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
    def test_feedback_single_question(self, mock_input, mock_repo, tmp_path):
        """Feedback with single question works correctly."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["42 requests per day", "s"]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Please provide feedback",
            "context": "",
            "decision_type": "feedback",
            "questions": [
                {"id": "q-1", "question": "Expected volume?", "answer": ""},
            ],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "submit_feedback"
        assert resolution["answers"]["q-1"] == "42 requests per day"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_feedback_empty_freetext_retry(self, mock_input, mock_repo, tmp_path):
        """Feedback with empty questions: empty text → retry → submit."""
        mock_repo.return_value = tmp_path
        # First attempt: empty text → retry, second: actual text → end
        mock_input.side_effect = ["", "r", "Now I have feedback", ""]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Please provide feedback",
            "context": "",
            "decision_type": "feedback",
            "questions": [],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "submit_feedback"
        assert "Now I have feedback" in resolution["answers"]["response"]


# ---------------------------------------------------------------------------
# Choice edge cases
# ---------------------------------------------------------------------------


class TestChoiceEdgeCases:
    """Tests for choice decision type edge cases."""

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.cancel_pipeline.return_value = {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_single_option(self, mock_input, mock_repo, tmp_path):
        """Choice with a single option selects it."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "1"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Proceed?",
            "context": "",
            "decision_type": "choice",
            "options": ["Yes"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["selected"] == "Yes"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_last_option_in_list(self, mock_input, mock_repo, tmp_path):
        """Selecting the last option in a list works correctly."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "4"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Which approach?",
            "context": "",
            "decision_type": "choice",
            "options": ["A", "B", "C", "D"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["selected"] == "D"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_choice_resolve_failure_retries(self, mock_input, mock_repo, tmp_path):
        """Choice selection fails to resolve, retries and succeeds."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["1", "1"]

        client = self._make_client()
        client.resolve_decision.side_effect = [
            OrchestratorError("server error", status_code=500),
            {"status": "resolved"},
        ]
        decision = {
            "id": "d1",
            "question": "Which?",
            "context": "",
            "decision_type": "choice",
            "options": ["PostgreSQL", "MongoDB"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        assert client.resolve_decision.call_count == 2


# ---------------------------------------------------------------------------
# Display and output tests
# ---------------------------------------------------------------------------


class TestDisplayOutput:
    """Tests for terminal display output content."""

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.cancel_pipeline.return_value = {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_phase_gate_shows_header(self, mock_input, mock_repo, tmp_path, capsys):
        """Phase gate shows HUMAN DECISION REQUIRED header."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "The refine phase has completed.",
            "context": "Draft",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        captured = capsys.readouterr()
        assert "HUMAN DECISION REQUIRED" in captured.out
        assert "issue-42" in captured.out

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_choice_shows_question(self, mock_input, mock_repo, tmp_path, capsys):
        """Choice type displays the question text."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "1"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Which database should we use?",
            "context": "",
            "decision_type": "choice",
            "options": ["PostgreSQL", "MongoDB"],
        }
        handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        captured = capsys.readouterr()
        assert "Which database should we use?" in captured.out

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_feedback_shows_question_count(self, mock_input, mock_repo, tmp_path, capsys):
        """Feedback type displays question text during collection."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["High", "Fast", "s"]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Please provide feedback",
            "context": "",
            "decision_type": "feedback",
            "questions": [
                {"id": "q-1", "question": "Expected volume?", "answer": ""},
                {"id": "q-2", "question": "Performance reqs?", "answer": ""},
            ],
        }
        handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        captured = capsys.readouterr()
        assert "Expected volume?" in captured.out
        assert "Performance reqs?" in captured.out

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_phase_gate_missing_draft_shows_not_found(
        self, mock_input, mock_repo, tmp_path, capsys
    ):
        """Phase gate with missing draft file shows 'not found' message."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "The plan phase has completed.",
            "context": "",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        captured = capsys.readouterr()
        assert "not found" in captured.out


# ---------------------------------------------------------------------------
# Default decision_type handling
# ---------------------------------------------------------------------------


class TestDefaultDecisionType:
    """Tests for default decision_type behavior when not specified."""

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.cancel_pipeline.return_value = {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_no_decision_type_defaults_to_choice(self, mock_input, mock_repo, tmp_path):
        """Decision without decision_type field defaults to 'choice'."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "1"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Pick one?",
            "context": "",
            "options": ["A", "B"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        call_args = client.resolve_decision.call_args
        resolution = json.loads(call_args[0][2])
        assert resolution["action"] == "select"
        assert resolution["selected"] == "A"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_no_decision_type_no_options_falls_to_generic(self, mock_input, mock_repo, tmp_path):
        """Decision without decision_type and no options falls to generic menu."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Approve the refine analysis?",
            "context": "Some context",
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        client.resolve_decision.assert_called_once_with("issue-42", "d1", "Approved")


# ---------------------------------------------------------------------------
# Contract decision bridge
# ---------------------------------------------------------------------------


class TestContractDecisionBridge:
    """Tests for the contract decision bridge (local-mode HITL)."""

    def _write_contract(self, tmp_path, key, decisions):
        """Helper: write a contract JSON with the given decisions list."""
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        contract = {
            "schemaVersion": "1.0",
            "issue": {"number": int(key) if key.isdigit() else 0},
            "decisions": decisions,
        }
        path = contracts_dir / f"{key}.json"
        path.write_text(json.dumps(contract, indent=2))
        return path

    # -- _get_contract_key --

    def test_get_contract_key_issue_mode(self):
        """Issue mode returns str(issue_number)."""
        assert _get_contract_key("issue", issue_number=42) == "42"

    def test_get_contract_key_issue_mode_no_number(self):
        """Issue mode with no issue_number returns None."""
        assert _get_contract_key("issue") is None

    def test_get_contract_key_local_mode(self):
        """Local mode returns pipeline_id."""
        assert _get_contract_key("local", pipeline_id="my-pipeline") == "my-pipeline"

    def test_get_contract_key_local_mode_no_pipeline(self):
        """Local mode with no pipeline_id returns None."""
        assert _get_contract_key("local") is None

    # -- _load_pending_contract_decisions --

    def test_no_contract_file(self, tmp_path):
        """Returns [] when contract file does not exist."""
        assert _load_pending_contract_decisions(tmp_path, "999") == []

    def test_empty_decisions(self, tmp_path):
        """Returns [] when decisions array is empty."""
        self._write_contract(tmp_path, "42", [])
        assert _load_pending_contract_decisions(tmp_path, "42") == []

    def test_all_resolved(self, tmp_path):
        """Filters out resolved decisions."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "Which DB?",
                    "type": "hitl",
                    "resolved": True,
                    "resolution": "PostgreSQL",
                    "options": [],
                },
            ],
        )
        assert _load_pending_contract_decisions(tmp_path, "42") == []

    def test_pending_decisions(self, tmp_path):
        """Returns only unresolved HITL decisions."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "Which DB?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [
                        {"id": "opt-1", "label": "PostgreSQL"},
                        {"id": "opt-2", "label": "MongoDB"},
                    ],
                },
                {
                    "id": "decision-2",
                    "question": "Done?",
                    "type": "auto",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
                {
                    "id": "decision-3",
                    "question": "Cache?",
                    "type": "hitl",
                    "resolved": True,
                    "resolution": "Redis",
                    "options": [],
                },
            ],
        )
        result = _load_pending_contract_decisions(tmp_path, "42")
        assert len(result) == 1
        assert result[0]["id"] == "decision-1"

    def test_malformed_json(self, tmp_path):
        """Returns [] on malformed JSON."""
        contracts_dir = tmp_path / ".egg-state" / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        (contracts_dir / "42.json").write_text("not json{")
        assert _load_pending_contract_decisions(tmp_path, "42") == []

    # -- _resolve_contract_decision --

    def test_resolve_decision(self, tmp_path):
        """Sets resolved/resolution/resolved_by/resolved_at, atomic write."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "Which DB?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
            ],
        )
        ok = _resolve_contract_decision(tmp_path, "42", "decision-1", "PostgreSQL")
        assert ok is True

        # Verify the file was updated
        contract_path = tmp_path / ".egg-state" / "contracts" / "42.json"
        data = json.loads(contract_path.read_text())
        d = data["decisions"][0]
        assert d["resolved"] is True
        assert d["resolution"] == "PostgreSQL"
        assert d["resolved_by"] == "human"
        assert d["resolved_at"] is not None

    def test_resolve_nonexistent(self, tmp_path):
        """Returns False for unknown decision ID."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "Which DB?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
            ],
        )
        ok = _resolve_contract_decision(tmp_path, "42", "decision-999", "anything")
        assert ok is False

    def test_resolve_missing_file(self, tmp_path):
        """Returns False when contract file does not exist."""
        ok = _resolve_contract_decision(tmp_path, "999", "decision-1", "anything")
        assert ok is False

    # -- Phase gate integration --

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_phase_gate_shows_q_option(self, mock_input, mock_repo, tmp_path, capsys):
        """[q] appears when pending contract decisions exist."""
        mock_repo.return_value = tmp_path
        # User selects "3" to approve, then "y" to confirm despite pending
        mock_input.side_effect = ["3", "y"]

        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "Which DB?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
            ],
        )

        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        decision = {
            "id": "d1",
            "question": "The refine phase has completed.",
            "context": "",
            "decision_type": "phase_gate",
        }
        handle_hitl_checkpoint(client, "issue-42", decision, pipeline_mode="issue", issue_number=42)

        captured = capsys.readouterr()
        assert "[q]" in captured.out
        assert "1 pending" in captured.out

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_phase_gate_no_q_without_decisions(self, mock_input, mock_repo, tmp_path, capsys):
        """[q] absent when no pending contract decisions exist."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"

        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        decision = {
            "id": "d1",
            "question": "The refine phase has completed.",
            "context": "",
            "decision_type": "phase_gate",
        }
        handle_hitl_checkpoint(client, "issue-42", decision, pipeline_mode="issue", issue_number=42)

        captured = capsys.readouterr()
        assert "[q]" not in captured.out

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_approve_with_pending_warns(self, mock_input, mock_repo, tmp_path, capsys):
        """Warning shown on approve with unanswered questions."""
        mock_repo.return_value = tmp_path
        # "3" to approve, "n" to cancel, then "3" again, "y" to confirm
        mock_input.side_effect = ["3", "n", "3", "y"]

        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "Which DB?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
            ],
        )

        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        decision = {
            "id": "d1",
            "question": "The refine phase has completed.",
            "context": "",
            "decision_type": "phase_gate",
        }
        result = handle_hitl_checkpoint(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        captured = capsys.readouterr()
        assert "still unanswered" in captured.out
        assert result == "resolved"

    # -- _handle_contract_questions unit tests --

    @patch("builtins.input")
    def test_handle_questions_option_based(self, mock_input, tmp_path, capsys):
        """Option-based question: user selects numbered choice."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "Which DB?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [
                        {"id": "opt-1", "label": "PostgreSQL"},
                        {"id": "opt-2", "label": "MongoDB"},
                    ],
                },
            ],
        )
        pending = _load_pending_contract_decisions(tmp_path, "42")
        mock_input.return_value = "1"

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "answered"
        # Verify decision was resolved
        data = json.loads((tmp_path / ".egg-state" / "contracts" / "42.json").read_text())
        assert data["decisions"][0]["resolved"] is True
        assert data["decisions"][0]["resolution"] == "PostgreSQL"

    @patch("builtins.input")
    def test_handle_questions_free_text(self, mock_input, tmp_path, capsys):
        """Free-text question: user types an answer."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "What is expected volume?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
            ],
        )
        pending = _load_pending_contract_decisions(tmp_path, "42")
        mock_input.return_value = "About 1000 requests per day"

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "answered"
        data = json.loads((tmp_path / ".egg-state" / "contracts" / "42.json").read_text())
        assert data["decisions"][0]["resolved"] is True
        assert data["decisions"][0]["resolution"] == "About 1000 requests per day"

    @patch("builtins.input")
    def test_handle_questions_skip(self, mock_input, tmp_path, capsys):
        """User skips a free-text question with /s."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "What is expected volume?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
            ],
        )
        pending = _load_pending_contract_decisions(tmp_path, "42")
        mock_input.return_value = "/s"

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "answered"
        # Decision should NOT be resolved (skipped)
        data = json.loads((tmp_path / ".egg-state" / "contracts" / "42.json").read_text())
        assert data["decisions"][0]["resolved"] is False

    @patch("builtins.input")
    def test_handle_questions_quit_free_text(self, mock_input, tmp_path, capsys):
        """User quits from free-text question with /q."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "What is expected volume?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
            ],
        )
        pending = _load_pending_contract_decisions(tmp_path, "42")
        mock_input.return_value = "/q"

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "quit"

    @patch("builtins.input")
    def test_handle_questions_quit_option_based(self, mock_input, tmp_path, capsys):
        """User quits from option-based question with q."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "Which DB?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [
                        {"id": "opt-1", "label": "PostgreSQL"},
                        {"id": "opt-2", "label": "MongoDB"},
                    ],
                },
            ],
        )
        pending = _load_pending_contract_decisions(tmp_path, "42")
        mock_input.return_value = "q"

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "quit"

    @patch("builtins.input")
    def test_handle_questions_skip_option_based(self, mock_input, tmp_path, capsys):
        """User skips an option-based question with s."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "Which DB?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [
                        {"id": "opt-1", "label": "PostgreSQL"},
                        {"id": "opt-2", "label": "MongoDB"},
                    ],
                },
            ],
        )
        pending = _load_pending_contract_decisions(tmp_path, "42")
        mock_input.return_value = "s"

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "answered"
        # Decision should NOT be resolved (skipped)
        data = json.loads((tmp_path / ".egg-state" / "contracts" / "42.json").read_text())
        assert data["decisions"][0]["resolved"] is False

    @patch("builtins.input")
    def test_handle_questions_eof_during_free_text(self, mock_input, tmp_path, capsys):
        """EOFError during free-text input returns quit."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "What is expected volume?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
            ],
        )
        pending = _load_pending_contract_decisions(tmp_path, "42")
        mock_input.side_effect = EOFError

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "quit"

    @patch("builtins.input")
    def test_handle_questions_keyboard_interrupt(self, mock_input, tmp_path, capsys):
        """KeyboardInterrupt during free-text input returns quit."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "What is expected volume?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
            ],
        )
        pending = _load_pending_contract_decisions(tmp_path, "42")
        mock_input.side_effect = KeyboardInterrupt

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "quit"

    @patch("egg_lib.sdlc_hitl._resolve_contract_decision")
    @patch("builtins.input")
    def test_handle_questions_resolve_failure(self, mock_input, mock_resolve, tmp_path, capsys):
        """When _resolve_contract_decision fails, error message is printed."""
        pending = [
            {
                "id": "decision-1",
                "question": "Which DB?",
                "type": "hitl",
                "resolved": False,
                "options": [{"label": "PostgreSQL"}, {"label": "MongoDB"}],
            },
        ]
        mock_input.return_value = "1"
        mock_resolve.return_value = False

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "answered"
        captured = capsys.readouterr()
        assert "Failed to save" in captured.out

    @patch("builtins.input")
    def test_handle_questions_literal_q_in_free_text(self, mock_input, tmp_path, capsys):
        """Literal 'q' as free-text answer is saved (not intercepted)."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "What letter?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
            ],
        )
        pending = _load_pending_contract_decisions(tmp_path, "42")
        mock_input.return_value = "q"

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "answered"
        data = json.loads((tmp_path / ".egg-state" / "contracts" / "42.json").read_text())
        assert data["decisions"][0]["resolved"] is True
        assert data["decisions"][0]["resolution"] == "q"

    @patch("builtins.input")
    def test_handle_questions_literal_s_in_free_text(self, mock_input, tmp_path, capsys):
        """Literal 's' as free-text answer is saved (not intercepted)."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "What letter?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
            ],
        )
        pending = _load_pending_contract_decisions(tmp_path, "42")
        mock_input.return_value = "s"

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "answered"
        data = json.loads((tmp_path / ".egg-state" / "contracts" / "42.json").read_text())
        assert data["decisions"][0]["resolved"] is True
        assert data["decisions"][0]["resolution"] == "s"

    # -- Full [q] → answer → approve flow --

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_q_answer_then_approve_flow(self, mock_input, mock_repo, tmp_path, capsys):
        """Full flow: [q] to answer questions, then [3] to approve."""
        mock_repo.return_value = tmp_path

        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "Which DB?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [
                        {"id": "opt-1", "label": "PostgreSQL"},
                        {"id": "opt-2", "label": "MongoDB"},
                    ],
                },
            ],
        )

        # "q" to enter questions, "1" to select PostgreSQL, then "3" to approve
        mock_input.side_effect = ["q", "1", "3"]

        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        decision = {
            "id": "d1",
            "question": "The refine phase has completed.",
            "context": "",
            "decision_type": "phase_gate",
        }
        result = handle_hitl_checkpoint(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        assert result == "resolved"
        # Verify the contract decision was resolved
        data = json.loads((tmp_path / ".egg-state" / "contracts" / "42.json").read_text())
        assert data["decisions"][0]["resolved"] is True
        assert data["decisions"][0]["resolution"] == "PostgreSQL"
        # After answering, approve should not warn about pending questions
        captured = capsys.readouterr()
        assert "still unanswered" not in captured.out

    # -- EOF handling in option-based questions --

    @patch("builtins.input")
    def test_handle_questions_eof_during_option_based(self, mock_input, tmp_path, capsys):
        """EOFError during option-based question returns quit (not ValueError)."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "Which DB?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [
                        {"id": "opt-1", "label": "PostgreSQL"},
                        {"id": "opt-2", "label": "MongoDB"},
                    ],
                },
            ],
        )
        pending = _load_pending_contract_decisions(tmp_path, "42")
        mock_input.side_effect = EOFError

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "quit"

    # -- Multi-question test --

    @patch("builtins.input")
    def test_handle_questions_multi_answer_one_skip_one(self, mock_input, tmp_path, capsys):
        """Multiple questions: answer the first, skip the second."""
        self._write_contract(
            tmp_path,
            "42",
            [
                {
                    "id": "decision-1",
                    "question": "Which DB?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [
                        {"id": "opt-1", "label": "PostgreSQL"},
                        {"id": "opt-2", "label": "MongoDB"},
                    ],
                },
                {
                    "id": "decision-2",
                    "question": "Expected volume?",
                    "type": "hitl",
                    "resolved": False,
                    "resolution": None,
                    "options": [],
                },
            ],
        )
        pending = _load_pending_contract_decisions(tmp_path, "42")
        # "1" selects PostgreSQL for the first question, "/s" skips the second
        mock_input.side_effect = ["1", "/s"]

        result = _handle_contract_questions(tmp_path, "42", pending)

        assert result == "answered"
        data = json.loads((tmp_path / ".egg-state" / "contracts" / "42.json").read_text())
        # First decision resolved
        assert data["decisions"][0]["resolved"] is True
        assert data["decisions"][0]["resolution"] == "PostgreSQL"
        # Second decision skipped (still unresolved)
        assert data["decisions"][1]["resolved"] is False


# ---------------------------------------------------------------------------
# _display_in_pager tests
# ---------------------------------------------------------------------------


class TestDisplayInPager:
    """Tests for the _display_in_pager function (glow-first pager)."""

    @patch("egg_lib.sdlc_hitl.shutil.which", return_value="/usr/bin/glow")
    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_glow_used_when_available(self, mock_run, mock_which, monkeypatch):
        """Uses glow -p when glow is on PATH and $PAGER is not set."""
        monkeypatch.delenv("PAGER", raising=False)
        mock_run.return_value = MagicMock(returncode=0)

        _display_in_pager("# Hello\nWorld")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "glow"
        assert cmd[1] == "-p"
        assert cmd[2].endswith(".md")

    @patch("egg_lib.sdlc_hitl.shutil.which", return_value="/usr/bin/glow")
    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_pager_env_skips_glow(self, mock_run, mock_which, monkeypatch):
        """$PAGER overrides glow — glow is never tried."""
        monkeypatch.setenv("PAGER", "more")
        mock_run.return_value = MagicMock(returncode=0)

        _display_in_pager("content")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "more"
        # glow should not have been invoked
        assert "glow" not in cmd

    @patch("egg_lib.sdlc_hitl.shutil.which", return_value="/usr/bin/glow")
    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_glow_failure_falls_back_to_less(self, mock_run, mock_which, monkeypatch):
        """When glow fails (non-zero exit), falls back to less -R."""
        monkeypatch.delenv("PAGER", raising=False)
        # First call (glow) fails, second call (less) succeeds
        mock_run.side_effect = [MagicMock(returncode=1), MagicMock(returncode=0)]

        _display_in_pager("content")

        assert mock_run.call_count == 2
        first_cmd = mock_run.call_args_list[0][0][0]
        assert first_cmd[0] == "glow"
        second_cmd = mock_run.call_args_list[1][0][0]
        assert second_cmd[0] == "less"
        assert second_cmd[1] == "-R"

    @patch("egg_lib.sdlc_hitl.shutil.which", return_value="/usr/bin/glow")
    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_glow_and_less_failure_prints_raw(self, mock_run, mock_which, monkeypatch, capsys):
        """When both glow and less fail, prints raw content."""
        monkeypatch.delenv("PAGER", raising=False)
        mock_run.return_value = MagicMock(returncode=1)

        _display_in_pager("fallback content")

        assert mock_run.call_count == 2  # glow, then less
        captured = capsys.readouterr()
        assert "fallback content" in captured.out

    @patch("egg_lib.sdlc_hitl.shutil.which", return_value=None)
    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_no_glow_uses_less(self, mock_run, mock_which, monkeypatch):
        """Without glow on PATH and no $PAGER, uses less -R."""
        monkeypatch.delenv("PAGER", raising=False)
        mock_run.return_value = MagicMock(returncode=0)

        _display_in_pager("# Heading")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "less"
        assert cmd[1] == "-R"

    @patch("egg_lib.sdlc_hitl.shutil.which", return_value=None)
    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_less_failure_prints_raw(self, mock_run, mock_which, monkeypatch, capsys):
        """When less fails and no glow, prints raw content."""
        monkeypatch.delenv("PAGER", raising=False)
        mock_run.side_effect = FileNotFoundError("less not found")

        _display_in_pager("fallback on missing less")

        captured = capsys.readouterr()
        assert "fallback on missing less" in captured.out

    @patch("egg_lib.sdlc_hitl.shutil.which", return_value=None)
    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_pager_failure_prints_raw(self, mock_run, mock_which, monkeypatch, capsys):
        """When $PAGER fails, prints raw content (no glow/less fallback)."""
        monkeypatch.setenv("PAGER", "bad-pager")
        mock_run.side_effect = FileNotFoundError("bad-pager not found")

        _display_in_pager("pager missing content")

        captured = capsys.readouterr()
        assert "pager missing content" in captured.out
        # Only one attempt — $PAGER doesn't cascade to glow/less
        mock_run.assert_called_once()

    @patch("egg_lib.sdlc_hitl.shutil.which", return_value="/usr/bin/glow")
    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_temp_file_has_md_suffix(self, mock_run, mock_which, monkeypatch):
        """Temp file has .md suffix so glow renders correctly."""
        monkeypatch.delenv("PAGER", raising=False)
        mock_run.return_value = MagicMock(returncode=0)

        _display_in_pager("# Test")

        cmd = mock_run.call_args[0][0]
        assert cmd[-1].endswith(".md")

    @patch("egg_lib.sdlc_hitl.shutil.which", return_value="/usr/bin/glow")
    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_passes_stdin_stdout_stderr(self, mock_run, mock_which, monkeypatch):
        """Passes stdin/stdout/stderr for TTY interaction."""
        monkeypatch.delenv("PAGER", raising=False)
        mock_run.return_value = MagicMock(returncode=0)

        _display_in_pager("content")

        kwargs = mock_run.call_args[1]
        assert kwargs["stdin"] is sys.stdin
        assert kwargs["stdout"] is sys.stdout
        assert kwargs["stderr"] is sys.stderr


# ---------------------------------------------------------------------------
# Phase gate [v] view option tests
# ---------------------------------------------------------------------------


class TestPhaseGateViewOption:
    """Tests for the [v] view full document option in phase gate menu."""

    def _make_client(self):
        client = MagicMock(spec=["resolve_decision", "cancel_pipeline"])
        client.resolve_decision.return_value = {"status": "resolved"}
        client.cancel_pipeline.return_value = {"status": "cancelled"}
        return client

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._display_in_pager")
    @patch("builtins.input")
    def test_view_option_opens_pager(self, mock_input, mock_pager, mock_repo, tmp_path):
        """[v] option opens the document in the pager, then returns to menu."""
        mock_repo.return_value = tmp_path
        # 'v' to view, then '3' to approve
        mock_input.side_effect = ["v", "3"]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "The refine phase has completed. Please review the analysis.",
            "context": "Full document content here",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        # Pager called twice: once on initial display, once on [v]
        assert mock_pager.call_count == 2
        mock_pager.assert_any_call("Full document content here")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._display_in_pager")
    @patch("builtins.input")
    def test_view_option_not_shown_without_draft(
        self, mock_input, mock_pager, mock_repo, tmp_path, capsys
    ):
        """[v] option is not available when there is no draft content."""
        mock_repo.return_value = tmp_path
        # 'v' is invalid (no draft), then '3' to approve
        mock_input.side_effect = ["v", "3"]

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "Ready to implement?",
            "context": "",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        # Pager should not have been called (no draft content)
        mock_pager.assert_not_called()
        # Prompt text should not advertise [v] when there is no draft
        captured = capsys.readouterr()
        assert "[v]" not in captured.out  # menu item not printed
        # Verify the prompt string passed to input() also omits /v
        prompt_args = [call.args[0] for call in mock_input.call_args_list if call.args]
        assert all("/v" not in p for p in prompt_args)

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._display_in_pager")
    @patch("builtins.input")
    def test_initial_pager_called_for_phase_gate(self, mock_input, mock_pager, mock_repo, tmp_path):
        """Phase gate calls pager (not preview) on initial display."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "3"

        client = self._make_client()
        decision = {
            "id": "d1",
            "question": "The refine phase has completed. Please review the analysis.",
            "context": "Analysis document content",
            "decision_type": "phase_gate",
            "options": ["approve", "request changes"],
        }
        result = handle_hitl_checkpoint(
            client,
            "issue-42",
            decision,
            pipeline_mode="issue",
            issue_number=42,
        )

        assert result == "resolved"
        mock_pager.assert_called_once_with("Analysis document content")
