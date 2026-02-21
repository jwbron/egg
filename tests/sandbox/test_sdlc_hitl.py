"""Tests for sandbox/egg_lib/sdlc_hitl.py - HITL checkpoint handling."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "sandbox"))

from egg_lib.orch_client import OrchestratorError
from egg_lib.sdlc_hitl import (
    _REQUIREMENTS_SYSTEM_PROMPT,
    _detect_phase,
    _find_repo_path,
    _get_draft_path,
    _launch_claude,
    _launch_requirements_claude,
    _read_draft,
    handle_hitl_checkpoint,
    handle_pre_refine,
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
# _launch_claude unit tests
# ---------------------------------------------------------------------------


class TestLaunchClaude:
    """Unit tests for _launch_claude command construction."""

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_with_draft_context(self, mock_run, tmp_path):
        """When draft_rel is provided, --append-system-prompt is added."""
        _launch_claude(tmp_path, draft_rel="drafts/42-analysis.md", phase="refine", issue_number=42)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "--append-system-prompt" in cmd
        prompt_idx = cmd.index("--append-system-prompt")
        prompt_text = cmd[prompt_idx + 1]
        assert "refine" in prompt_text
        assert "#42" in prompt_text
        assert "drafts/42-analysis.md" in prompt_text

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_without_draft_context(self, mock_run, tmp_path):
        """When draft_rel is None, bare 'claude' command is used."""
        _launch_claude(tmp_path, draft_rel=None, phase="implement", issue_number=10)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["claude"]
        assert mock_run.call_args[1]["cwd"] == str(tmp_path)

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_with_draft_but_no_phase_or_issue(self, mock_run, tmp_path):
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


# ---------------------------------------------------------------------------
# _detect_phase – pre_refine recognition (task-3-3)
# ---------------------------------------------------------------------------


class TestDetectPhasePreRefine:
    """Tests that _detect_phase recognises pre_refine before refine."""

    def test_pre_refine_hyphen(self):
        assert _detect_phase("Approve the pre-refine draft") == "pre_refine"

    def test_pre_refine_underscore(self):
        assert _detect_phase("Review pre_refine step") == "pre_refine"

    def test_pre_refine_case_insensitive(self):
        assert _detect_phase("PRE-REFINE review needed") == "pre_refine"

    def test_requirements_gathering_keyword(self):
        assert _detect_phase("Ready for requirements gathering?") == "pre_refine"

    def test_pre_refine_trumps_refine(self):
        """A question containing 'pre-refine' must NOT match 'refine'."""
        result = _detect_phase("Approve the pre-refine analysis?")
        assert result == "pre_refine"

    def test_refine_still_works(self):
        """Plain 'refine' without 'pre-' prefix still maps to refine."""
        assert _detect_phase("Approve the refine analysis?") == "refine"

    def test_pre_refine_with_context_arg(self):
        """Context arg is accepted but detection is question-based."""
        assert _detect_phase("pre-refine step ready", "some context") == "pre_refine"

    def test_requirements_gathering_case_insensitive(self):
        assert _detect_phase("REQUIREMENTS GATHERING in progress") == "pre_refine"


# ---------------------------------------------------------------------------
# _get_draft_path – pre_refine branch (task-3-5)
# ---------------------------------------------------------------------------


class TestGetDraftPathPreRefine:
    """Tests for pre_refine explicit branch in _get_draft_path."""

    def test_pre_refine_issue_mode(self):
        path = _get_draft_path("pre_refine", "issue", issue_number=42)
        assert path == ".egg-state/drafts/42-requirements.md"

    def test_pre_refine_local_mode(self):
        path = _get_draft_path("pre_refine", "local", pipeline_id="local-abc")
        assert path == ".egg-state/drafts/local-abc-requirements.md"

    def test_pre_refine_local_no_pipeline_id(self):
        path = _get_draft_path("pre_refine", "local")
        assert path == ".egg-state/drafts/local-requirements.md"

    def test_pre_refine_no_issue_number(self):
        path = _get_draft_path("pre_refine", "issue")
        assert path == ".egg-state/drafts/unknown-requirements.md"

    def test_pre_refine_does_not_use_generic_fallback(self):
        """Ensures pre_refine hits the explicit branch, not the else clause
        which would produce {prefix}-pre_refine.md."""
        path = _get_draft_path("pre_refine", "issue", issue_number=10)
        assert "requirements" in path
        assert "pre_refine" not in path


# ---------------------------------------------------------------------------
# _launch_requirements_claude tests (task-3-2)
# ---------------------------------------------------------------------------


class TestLaunchRequirementsClaude:
    """Tests for _launch_requirements_claude command construction."""

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_basic_command_structure(self, mock_run, tmp_path):
        """The command includes --append-system-prompt and --disallowedTools."""
        _launch_requirements_claude(tmp_path, "drafts/42-requirements.md")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "--append-system-prompt" in cmd
        assert "--disallowedTools" in cmd

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_disallowed_tools(self, mock_run, tmp_path):
        """Read, Glob, Grep, Bash are disallowed to prevent codebase access."""
        _launch_requirements_claude(tmp_path, "drafts/42-requirements.md")
        cmd = mock_run.call_args[0][0]
        tools_idx = cmd.index("--disallowedTools")
        tools_val = cmd[tools_idx + 1]
        assert "Read" in tools_val
        assert "Glob" in tools_val
        assert "Grep" in tools_val
        assert "Bash" in tools_val

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_system_prompt_contains_draft_path(self, mock_run, tmp_path):
        """The system prompt references the draft file path."""
        _launch_requirements_claude(tmp_path, "drafts/42-requirements.md")
        cmd = mock_run.call_args[0][0]
        prompt_idx = cmd.index("--append-system-prompt")
        prompt_text = cmd[prompt_idx + 1]
        assert "drafts/42-requirements.md" in prompt_text

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_system_prompt_contains_requirements_structure(self, mock_run, tmp_path):
        """The system prompt guides toward problem statement, functional reqs, etc."""
        _launch_requirements_claude(tmp_path, "drafts/42-requirements.md")
        cmd = mock_run.call_args[0][0]
        prompt_idx = cmd.index("--append-system-prompt")
        prompt_text = cmd[prompt_idx + 1]
        assert "Problem Statement" in prompt_text
        assert "Functional Requirements" in prompt_text
        assert "Constraints" in prompt_text
        assert "Acceptance Criteria" in prompt_text

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_issue_number_appended(self, mock_run, tmp_path):
        """When issue_number is set, it appears in the prompt."""
        _launch_requirements_claude(tmp_path, "drafts/42-requirements.md", issue_number=42)
        cmd = mock_run.call_args[0][0]
        prompt_idx = cmd.index("--append-system-prompt")
        prompt_text = cmd[prompt_idx + 1]
        assert "#42" in prompt_text

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_context_appended(self, mock_run, tmp_path):
        """When context is provided, it's included in the prompt."""
        _launch_requirements_claude(
            tmp_path, "drafts/42-requirements.md", context="Fix login bug"
        )
        cmd = mock_run.call_args[0][0]
        prompt_idx = cmd.index("--append-system-prompt")
        prompt_text = cmd[prompt_idx + 1]
        assert "Fix login bug" in prompt_text
        assert "Context from issue" in prompt_text

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_no_issue_no_context(self, mock_run, tmp_path):
        """Without issue_number or context, prompt still works."""
        _launch_requirements_claude(tmp_path, "drafts/42-requirements.md")
        cmd = mock_run.call_args[0][0]
        prompt_idx = cmd.index("--append-system-prompt")
        prompt_text = cmd[prompt_idx + 1]
        assert "Issue: #" not in prompt_text
        assert "Context from issue" not in prompt_text

    @patch("egg_lib.sdlc_hitl.subprocess.run")
    def test_cwd_is_repo_path(self, mock_run, tmp_path):
        """subprocess.run is called with cwd=repo_path."""
        _launch_requirements_claude(tmp_path, "drafts/42-requirements.md")
        kwargs = mock_run.call_args[1]
        assert kwargs["cwd"] == str(tmp_path)

    @patch("egg_lib.sdlc_hitl.subprocess.run", side_effect=FileNotFoundError)
    def test_claude_not_found(self, mock_run, tmp_path, capsys):
        """FileNotFoundError is caught gracefully."""
        _launch_requirements_claude(tmp_path, "drafts/42-requirements.md")
        captured = capsys.readouterr()
        assert "not found" in captured.out

    @patch("egg_lib.sdlc_hitl.subprocess.run", side_effect=RuntimeError("boom"))
    def test_generic_exception(self, mock_run, tmp_path, capsys):
        """Other exceptions are caught and reported."""
        _launch_requirements_claude(tmp_path, "drafts/42-requirements.md")
        captured = capsys.readouterr()
        assert "Failed to launch Claude" in captured.out


# ---------------------------------------------------------------------------
# _REQUIREMENTS_SYSTEM_PROMPT tests (task-3-2)
# ---------------------------------------------------------------------------


class TestRequirementsSystemPrompt:
    """Tests for the prompt template constant."""

    def test_has_format_placeholder(self):
        assert "{draft_path}" in _REQUIREMENTS_SYSTEM_PROMPT

    def test_format_with_path(self):
        formatted = _REQUIREMENTS_SYSTEM_PROMPT.format(draft_path="drafts/10-requirements.md")
        assert "drafts/10-requirements.md" in formatted

    def test_contains_no_codebase_access_rule(self):
        assert "Do NOT access or read any source code" in _REQUIREMENTS_SYSTEM_PROMPT

    def test_contains_required_sections(self):
        assert "Problem Statement" in _REQUIREMENTS_SYSTEM_PROMPT
        assert "Functional Requirements" in _REQUIREMENTS_SYSTEM_PROMPT
        assert "Constraints" in _REQUIREMENTS_SYSTEM_PROMPT
        assert "Acceptance Criteria" in _REQUIREMENTS_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# handle_pre_refine tests (task-3-1)
# ---------------------------------------------------------------------------


class TestHandlePreRefine:
    """Tests for the pre-refine HITL handler."""

    def _make_decision(self, **overrides):
        base = {
            "id": "d-pre-1",
            "question": "Approve the pre-refine requirements?",
            "context": "Implement authentication feature",
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
    def test_approve_with_existing_draft(self, mock_input, mock_repo, tmp_path, capsys):
        """Option 4 approves when requirements document exists."""
        # Create draft
        draft_dir = tmp_path / ".egg-state" / "drafts"
        draft_dir.mkdir(parents=True)
        (draft_dir / "42-requirements.md").write_text("# Requirements\n\nSome content")

        mock_repo.return_value = tmp_path
        mock_input.return_value = "4"

        client = self._make_client()
        decision = self._make_decision()

        result = handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        assert result == "resolved"
        client.resolve_decision.assert_called_once_with("issue-42", "d-pre-1", "Approved")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_approve_without_draft_returns_to_menu(self, mock_input, mock_repo, tmp_path, capsys):
        """Option 4 rejects when no requirements document exists, returns to menu."""
        mock_repo.return_value = tmp_path
        # First try approve (no doc) → returns to menu → cancel
        mock_input.side_effect = ["4", "5"]

        client = self._make_client()
        decision = self._make_decision()

        result = handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        assert result == "cancelled"
        client.resolve_decision.assert_not_called()
        captured = capsys.readouterr()
        assert "No requirements document found" in captured.out

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_cancel(self, mock_input, mock_repo, tmp_path, capsys):
        """Option 5 cancels the pipeline."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "5"

        client = self._make_client()
        decision = self._make_decision()

        result = handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        assert result == "cancelled"
        client.cancel_pipeline.assert_called_once_with("issue-42")

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._launch_requirements_claude")
    @patch("builtins.input")
    def test_launch_claude_requirements(
        self, mock_input, mock_claude, mock_repo, tmp_path, capsys
    ):
        """Option 1 launches Claude for requirements gathering, then returns to menu."""
        mock_repo.return_value = tmp_path
        # Create draft so approve succeeds
        draft_dir = tmp_path / ".egg-state" / "drafts"
        draft_dir.mkdir(parents=True)
        (draft_dir / "42-requirements.md").write_text("# Requirements\n\nContent")

        mock_input.side_effect = ["1", "4"]

        client = self._make_client()
        decision = self._make_decision()

        result = handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        assert result == "resolved"
        mock_claude.assert_called_once_with(
            tmp_path,
            ".egg-state/drafts/42-requirements.md",
            42,
            "Implement authentication feature",
        )

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._launch_editor")
    @patch("builtins.input")
    def test_edit_creates_template_when_no_draft(
        self, mock_input, mock_editor, mock_repo, tmp_path, capsys
    ):
        """Option 2 creates a template requirements file if none exists."""
        mock_repo.return_value = tmp_path
        mock_editor.return_value = True
        # Edit → editor opens → back to menu → cancel
        mock_input.side_effect = ["2", "5"]

        client = self._make_client()
        decision = self._make_decision()

        handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        draft_file = tmp_path / ".egg-state" / "drafts" / "42-requirements.md"
        assert draft_file.exists()
        content = draft_file.read_text()
        assert "# Requirements" in content
        assert "## Problem Statement" in content
        mock_editor.assert_called_once_with(draft_file)

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("egg_lib.sdlc_hitl._launch_editor")
    @patch("builtins.input")
    def test_edit_existing_draft(
        self, mock_input, mock_editor, mock_repo, tmp_path, capsys
    ):
        """Option 2 opens editor on existing draft without overwriting."""
        draft_dir = tmp_path / ".egg-state" / "drafts"
        draft_dir.mkdir(parents=True)
        draft_file = draft_dir / "42-requirements.md"
        draft_file.write_text("# Existing requirements")

        mock_repo.return_value = tmp_path
        mock_editor.return_value = True
        mock_input.side_effect = ["2", "5"]

        client = self._make_client()
        decision = self._make_decision()

        handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        mock_editor.assert_called_once_with(draft_file)
        # Original content should not be overwritten before editor opens
        assert draft_file.read_text() == "# Existing requirements"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_view_with_draft(self, mock_input, mock_repo, tmp_path, capsys):
        """Option 3 displays the current requirements document."""
        draft_dir = tmp_path / ".egg-state" / "drafts"
        draft_dir.mkdir(parents=True)
        (draft_dir / "42-requirements.md").write_text("# Requirements\n\nLogin feature")

        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["3", "5"]

        client = self._make_client()
        decision = self._make_decision()

        handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        captured = capsys.readouterr()
        assert "Draft Preview" in captured.out
        assert "Requirements" in captured.out

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_view_without_draft(self, mock_input, mock_repo, tmp_path, capsys):
        """Option 3 shows message when no document exists."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["3", "5"]

        client = self._make_client()
        decision = self._make_decision()

        handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        captured = capsys.readouterr()
        assert "No requirements document yet" in captured.out

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_header_shows_pipeline_and_issue(self, mock_input, mock_repo, tmp_path, capsys):
        """The header displays pipeline ID, issue number, and PRE-REFINE title."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "5"

        client = self._make_client()
        decision = self._make_decision()

        handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        captured = capsys.readouterr()
        assert "PRE-REFINE" in captured.out
        assert "issue-42" in captured.out
        assert "#42" in captured.out

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_context_preview_shown(self, mock_input, mock_repo, tmp_path, capsys):
        """When decision has context, a preview is shown."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "5"

        client = self._make_client()
        decision = self._make_decision(context="Line1\nLine2\nLine3")

        handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        captured = capsys.readouterr()
        assert "Issue context" in captured.out
        assert "Line1" in captured.out

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_eof_cancels(self, mock_input, mock_repo, tmp_path, capsys):
        """EOFError during input defaults to cancel (option 5)."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = EOFError

        client = self._make_client()
        decision = self._make_decision()

        result = handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        assert result == "cancelled"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_resolve_failure_retries(self, mock_input, mock_repo, tmp_path, capsys):
        """If resolve_decision fails, user returns to menu."""
        draft_dir = tmp_path / ".egg-state" / "drafts"
        draft_dir.mkdir(parents=True)
        (draft_dir / "42-requirements.md").write_text("# Requirements\nContent")

        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["4", "4"]

        client = self._make_client()
        client.resolve_decision.side_effect = [
            OrchestratorError("server error", status_code=500),
            {"status": "resolved"},
        ]
        decision = self._make_decision()

        result = handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        assert result == "resolved"
        assert client.resolve_decision.call_count == 2

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_cancel_failure_still_returns_cancelled(
        self, mock_input, mock_repo, tmp_path, capsys
    ):
        """If cancel_pipeline fails, still returns 'cancelled'."""
        mock_repo.return_value = tmp_path
        mock_input.return_value = "5"

        client = self._make_client()
        client.cancel_pipeline.side_effect = OrchestratorError("fail", status_code=500)
        decision = self._make_decision()

        result = handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        assert result == "cancelled"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_approve_empty_draft_rejected(self, mock_input, mock_repo, tmp_path, capsys):
        """An empty (whitespace-only) draft is treated as no draft for approval."""
        draft_dir = tmp_path / ".egg-state" / "drafts"
        draft_dir.mkdir(parents=True)
        (draft_dir / "42-requirements.md").write_text("   \n  \n  ")

        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["4", "5"]

        client = self._make_client()
        decision = self._make_decision()

        result = handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        assert result == "cancelled"
        client.resolve_decision.assert_not_called()

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_invalid_choice_retries(self, mock_input, mock_repo, tmp_path, capsys):
        """Invalid menu choices prompt again."""
        mock_repo.return_value = tmp_path
        mock_input.side_effect = ["x", "9", "5"]

        client = self._make_client()
        decision = self._make_decision()

        result = handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        assert result == "cancelled"

    @patch("egg_lib.sdlc_hitl._find_repo_path")
    @patch("builtins.input")
    def test_draft_line_count_shown(self, mock_input, mock_repo, tmp_path, capsys):
        """When draft exists, its line count is displayed."""
        draft_dir = tmp_path / ".egg-state" / "drafts"
        draft_dir.mkdir(parents=True)
        (draft_dir / "42-requirements.md").write_text("line1\nline2\nline3")

        mock_repo.return_value = tmp_path
        mock_input.return_value = "5"

        client = self._make_client()
        decision = self._make_decision()

        handle_pre_refine(
            client, "issue-42", decision, pipeline_mode="issue", issue_number=42
        )

        captured = capsys.readouterr()
        assert "3 lines" in captured.out
