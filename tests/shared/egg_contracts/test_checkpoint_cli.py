"""Tests for checkpoint CLI."""

import argparse
import json
import subprocess
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from egg_config.constants import TEST_GATEWAY_PORT
from egg_contracts.checkpoint_cli import (
    _cmd_search_http,
    _get_checkpoint_repo_from_args,
    _search_checkpoint_transcript,
    cmd_cost,
    cmd_search,
    create_parser,
    main,
)
from egg_contracts.checkpoints import (
    AgentType,
    CheckpointIndexV2,
    CheckpointSummaryV2,
    CheckpointV2,
    Message,
    MessageRole,
    SessionMetadata,
    SessionStatus,
    TokenUsage,
    Transcript,
    TriggerType,
)

_TEST_GATEWAY_URL = f"http://gateway:{TEST_GATEWAY_PORT}"


def _make_summary(
    checkpoint_id: str,
    pipeline_id: str = "issue-745",
    phase: str = "implement",
    agent_type: AgentType = AgentType.CODER,
    total_tokens: int = 10000,
) -> CheckpointSummaryV2:
    """Create a checkpoint summary for testing."""
    return CheckpointSummaryV2(
        id=checkpoint_id,
        trigger_type=TriggerType.SESSION_END,
        session_status=SessionStatus.COMPLETED,
        session_id="session-1",
        pipeline_id=pipeline_id,
        pipeline_phase=phase,
        agent_type=agent_type,
        total_tokens=total_tokens,
        created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
    )


def _make_checkpoint(
    checkpoint_id: str,
    phase: str = "implement",
    agent_type: AgentType = AgentType.CODER,
    input_tokens: int = 8000,
    output_tokens: int = 2000,
    model: str = "claude-opus-4-5-20251101",
) -> CheckpointV2:
    """Create a full checkpoint for testing."""
    return CheckpointV2(
        id=checkpoint_id,
        trigger_type=TriggerType.SESSION_END,
        session_status=SessionStatus.COMPLETED,
        session_id="session-1",
        pipeline_phase=phase,
        pipeline_id="issue-745",
        agent_type=agent_type,
        session=SessionMetadata(
            session_id="session-1",
            started_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            model=model,
        ),
        token_usage=TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            total_tokens=input_tokens + output_tokens,
        ),
        created_at=datetime(2026, 1, 15, 12, 30, 0, tzinfo=UTC),
        session_started_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
    )


class TestCostCommand:
    """Tests for the cost subcommand."""

    def test_cost_parser_registered(self):
        """cost subcommand is registered in the parser."""
        parser = create_parser()
        args = parser.parse_args(["cost", "--pipeline", "issue-745"])
        assert args.command == "cost"
        assert args.pipeline == "issue-745"

    def test_cost_parser_all_filters(self):
        """cost subcommand accepts --pipeline, --issue, --pr filters."""
        parser = create_parser()
        args = parser.parse_args(["cost", "--issue", "745", "--pr", "42"])
        assert args.issue == 745
        assert args.pr == 42

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_cost_aggregation(self, mock_ref, mock_index, mock_filter, mock_load, _mock_gw, capsys):
        """cost subcommand aggregates token usage by phase and agent."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = CheckpointIndexV2(
            schemaVersion="2.0",
            checkpoints=[],
            last_updated=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )

        summaries = [
            _make_summary("ckpt-aaa11111", phase="plan", agent_type=AgentType.ARCHITECT),
            _make_summary("ckpt-bbb22222", phase="implement", agent_type=AgentType.CODER),
            _make_summary("ckpt-ccc33333", phase="implement", agent_type=AgentType.TESTER),
        ]
        mock_filter.return_value = summaries

        checkpoints = [
            _make_checkpoint(
                "ckpt-aaa11111",
                phase="plan",
                agent_type=AgentType.ARCHITECT,
                input_tokens=10000,
                output_tokens=5000,
            ),
            _make_checkpoint(
                "ckpt-bbb22222",
                phase="implement",
                agent_type=AgentType.CODER,
                input_tokens=50000,
                output_tokens=20000,
            ),
            _make_checkpoint(
                "ckpt-ccc33333",
                phase="implement",
                agent_type=AgentType.TESTER,
                input_tokens=20000,
                output_tokens=8000,
            ),
        ]
        mock_load.side_effect = checkpoints

        parser = create_parser()
        args = parser.parse_args(["cost", "--pipeline", "issue-745"])
        result = cmd_cost(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "Pipeline: issue-745" in output
        assert "Checkpoints: 3" in output
        assert "architect" in output
        assert "coder" in output
        assert "tester" in output
        assert "TOTAL" in output

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_cost_json_output(self, mock_ref, mock_index, mock_filter, mock_load, _mock_gw, capsys):
        """cost --json outputs structured JSON with breakdown."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = CheckpointIndexV2(
            schemaVersion="2.0",
            checkpoints=[],
            last_updated=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )

        summaries = [
            _make_summary("ckpt-ddd44444", phase="implement", agent_type=AgentType.CODER),
        ]
        mock_filter.return_value = summaries

        checkpoint = _make_checkpoint(
            "ckpt-ddd44444",
            input_tokens=100000,
            output_tokens=40000,
        )
        mock_load.return_value = checkpoint

        parser = create_parser()
        args = parser.parse_args(["cost", "--pipeline", "issue-745", "--json"])
        result = cmd_cost(args)

        assert result == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["pipeline_id"] == "issue-745"
        assert data["checkpoint_count"] == 1
        assert data["total_input_tokens"] == 100000
        assert data["total_output_tokens"] == 40000
        assert data["total_cost_usd"] > 0
        assert len(data["breakdown"]) == 1
        assert data["breakdown"][0]["phase"] == "implement"
        assert data["breakdown"][0]["agent"] == "coder"

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_cost_no_checkpoints(self, mock_ref, _mock_gw, capsys):
        """cost returns 0 with message when no checkpoint branch exists."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["cost", "--pipeline", "issue-745"])
        result = cmd_cost(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "No checkpoints found" in output

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_cost_skips_checkpoints_without_token_usage(
        self, mock_ref, mock_index, mock_filter, mock_load, _mock_gw, capsys
    ):
        """Checkpoints without token_usage are skipped in cost calculation."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = CheckpointIndexV2(
            schemaVersion="2.0",
            checkpoints=[],
            last_updated=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )

        summaries = [
            _make_summary("ckpt-eee55555"),
        ]
        mock_filter.return_value = summaries

        # Checkpoint with no token_usage
        checkpoint = _make_checkpoint("ckpt-eee55555")
        checkpoint.token_usage = None
        mock_load.return_value = checkpoint

        parser = create_parser()
        args = parser.parse_args(["cost", "--pipeline", "issue-745"])
        result = cmd_cost(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "No checkpoints with token usage data found" in output

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    def test_cost_via_main(self, _mock_gw):
        """cost subcommand is reachable via main()."""
        with patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref") as mock_ref:
            mock_ref.return_value = None
            result = main(["cost", "--pipeline", "issue-745"])
            assert result == 0


class TestGetCheckpointRepoFromArgs:
    """Tests for auto-detection of checkpoint_repo from repo config."""

    @pytest.fixture(autouse=True)
    def _clear_checkpoint_env(self, monkeypatch):
        """Ensure EGG_CHECKPOINT_REPO is unset unless a test explicitly sets it."""
        monkeypatch.delenv("EGG_CHECKPOINT_REPO", raising=False)

    def _make_args(self, **kwargs) -> argparse.Namespace:
        defaults = {"checkpoint_repo": None, "repo_path": "/tmp/test-repo"}
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_explicit_flag_takes_priority(self):
        """--checkpoint-repo flag is returned without auto-detection."""
        args = self._make_args(checkpoint_repo="owner/explicit-repo")
        checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
        assert checkpoint_repo == "owner/explicit-repo"
        assert source_repo is None

    @patch("config.repo_config.get_checkpoint_repo", return_value="jwbron/egg-checkpoints")
    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_auto_detects_from_https_remote(self, mock_git, mock_config):
        """Auto-detects checkpoint_repo from HTTPS remote URL."""
        mock_git.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/jwbron/egg.git\n",
            stderr="",
        )
        args = self._make_args()
        checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
        assert checkpoint_repo == "jwbron/egg-checkpoints"
        assert source_repo == "jwbron/egg"
        mock_config.assert_called_once_with("jwbron/egg")

    @patch("config.repo_config.get_checkpoint_repo", return_value="jwbron/egg-checkpoints")
    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_auto_detects_from_ssh_remote(self, mock_git, mock_config):
        """Auto-detects checkpoint_repo from SSH remote URL."""
        mock_git.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="git@github.com:jwbron/egg.git\n",
            stderr="",
        )
        args = self._make_args()
        checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
        assert checkpoint_repo == "jwbron/egg-checkpoints"
        assert source_repo == "jwbron/egg"
        mock_config.assert_called_once_with("jwbron/egg")

    @patch("config.repo_config.get_checkpoint_repo", return_value=None)
    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_returns_none_when_no_config(self, mock_git, mock_config):
        """Returns None checkpoint_repo when repo has no checkpoint_repo configured."""
        mock_git.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/owner/repo.git\n",
            stderr="",
        )
        args = self._make_args()
        checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
        assert checkpoint_repo is None
        assert source_repo == "owner/repo"

    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_returns_none_when_git_remote_fails(self, mock_git):
        """Returns None when git remote get-url fails."""
        mock_git.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="fatal: not a git repo",
        )
        args = self._make_args()
        checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
        assert checkpoint_repo is None
        assert source_repo is None

    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_returns_none_when_remote_url_not_github(self, mock_git):
        """Returns None when remote URL is not a GitHub URL."""
        mock_git.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://gitlab.com/owner/repo.git\n",
            stderr="",
        )
        args = self._make_args()
        checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
        assert checkpoint_repo is None
        assert source_repo is None

    @patch("config.repo_config.get_checkpoint_repo", return_value="org/dotted.repo-checkpoints")
    @patch("egg_contracts.checkpoint_cli.run_git")
    def test_auto_detects_repo_with_dots_in_name(self, mock_git, mock_config):
        """Auto-detects checkpoint_repo when repo name contains dots."""
        mock_git.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="https://github.com/my-org/some.project.git\n",
            stderr="",
        )
        args = self._make_args()
        checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
        assert checkpoint_repo == "org/dotted.repo-checkpoints"
        assert source_repo == "my-org/some.project"
        mock_config.assert_called_once_with("my-org/some.project")

    @patch("egg_contracts.checkpoint_cli.run_git", side_effect=Exception("timeout"))
    def test_returns_none_on_unexpected_error(self, mock_git):
        """Returns None gracefully on unexpected exceptions."""
        args = self._make_args()
        checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
        assert checkpoint_repo is None
        assert source_repo is None

    @patch.dict("os.environ", {"EGG_CHECKPOINT_REPO": "org/checkpoints"})
    def test_env_var_takes_priority_over_config(self):
        """EGG_CHECKPOINT_REPO env var is used when no CLI flag is given."""
        args = self._make_args()
        checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
        assert checkpoint_repo == "org/checkpoints"
        assert source_repo is None

    @patch.dict("os.environ", {"EGG_CHECKPOINT_REPO": "org/checkpoints"})
    def test_cli_flag_takes_priority_over_env_var(self):
        """--checkpoint-repo flag overrides EGG_CHECKPOINT_REPO env var."""
        args = self._make_args(checkpoint_repo="owner/explicit-repo")
        checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
        assert checkpoint_repo == "owner/explicit-repo"
        assert source_repo is None

    @patch.dict("os.environ", {"EGG_CHECKPOINT_REPO": "bad format"})
    def test_env_var_invalid_format_falls_through(self):
        """EGG_CHECKPOINT_REPO with invalid format is ignored (falls through)."""
        with patch("egg_contracts.checkpoint_cli.run_git") as mock_git:
            mock_git.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="fatal: not a git repo",
            )
            args = self._make_args()
            checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
            # Invalid env var is ignored, falls through to git remote (which also fails)
            assert checkpoint_repo is None
            assert source_repo is None

    def test_env_var_not_set_falls_through(self):
        """When EGG_CHECKPOINT_REPO is not set, falls through to config lookup."""
        with (
            patch("egg_contracts.checkpoint_cli.run_git") as mock_git,
            patch(
                "config.repo_config.get_checkpoint_repo",
                return_value="org/cp",
            ),
        ):
            mock_git.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="https://github.com/org/repo.git\n",
                stderr="",
            )
            args = self._make_args()
            checkpoint_repo, source_repo = _get_checkpoint_repo_from_args(args)
            assert checkpoint_repo == "org/cp"
            assert source_repo == "org/repo"


class TestSearchParser:
    """Tests for the search subcommand parser."""

    def test_search_parser_registered(self):
        """search subcommand is registered in the parser."""
        parser = create_parser()
        args = parser.parse_args(["search", "--text", "hello"])
        assert args.command == "search"
        assert args.text == "hello"

    def test_search_parser_default_limit(self):
        """search subcommand defaults to limit 20."""
        parser = create_parser()
        args = parser.parse_args(["search", "--text", "test"])
        assert args.limit == 20

    def test_search_parser_all_filter_flags(self):
        """search subcommand accepts all list filter flags."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "search",
                "--text",
                "query",
                "--issue",
                "42",
                "--pr",
                "10",
                "--branch",
                "egg/feature",
                "--pipeline",
                "issue-42",
                "--agent-type",
                "coder",
                "--phase",
                "implement",
                "--status",
                "completed",
                "--limit",
                "5",
                "--json",
            ]
        )
        assert args.text == "query"
        assert args.issue == 42
        assert args.pr == 10
        assert args.branch == "egg/feature"
        assert args.pipeline == "issue-42"
        assert args.agent_type == "coder"
        assert args.phase == "implement"
        assert args.status == "completed"
        assert args.limit == 5
        assert args.json is True

    def test_search_parser_text_required(self):
        """search subcommand requires --text."""
        parser = create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["search"])


def _make_transcript_checkpoint(
    checkpoint_id: str,
    messages: list[Message] | None = None,
) -> CheckpointV2:
    """Create a checkpoint with transcript data for search testing."""
    transcript = None
    if messages is not None:
        transcript = Transcript(
            messages=messages,
            message_count=len(messages),
        )
    return CheckpointV2(
        id=checkpoint_id,
        trigger_type=TriggerType.SESSION_END,
        session_status=SessionStatus.COMPLETED,
        session_id="session-1",
        session=SessionMetadata(
            session_id="session-1",
            started_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        ),
        transcript=transcript,
        created_at=datetime(2026, 1, 15, 12, 30, 0, tzinfo=UTC),
        session_started_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
    )


class TestSearchCheckpointTranscript:
    """Tests for _search_checkpoint_transcript."""

    def test_match_found(self):
        """Finds matching text in message content."""
        messages = [
            Message(
                role=MessageRole.USER,
                content="Please fix issue 898 in the auth module",
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            ),
        ]
        cp = _make_transcript_checkpoint("ckpt-aa00aa00", messages=messages)
        snippets = _search_checkpoint_transcript(cp, "issue 898")
        assert len(snippets) == 1
        assert "issue 898" in snippets[0]
        assert "[user]" in snippets[0]

    def test_case_insensitive(self):
        """Search is case-insensitive."""
        messages = [
            Message(
                role=MessageRole.ASSISTANT,
                content="Looking at Issue 898 now",
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            ),
        ]
        cp = _make_transcript_checkpoint("ckpt-bb00bb00", messages=messages)
        snippets = _search_checkpoint_transcript(cp, "issue 898")
        assert len(snippets) == 1

    def test_no_transcript(self):
        """Returns empty list when checkpoint has no transcript."""
        cp = _make_transcript_checkpoint("ckpt-cc00cc00", messages=None)
        snippets = _search_checkpoint_transcript(cp, "anything")
        assert snippets == []

    def test_no_match(self):
        """Returns empty list when text is not found."""
        messages = [
            Message(
                role=MessageRole.USER,
                content="Something unrelated",
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            ),
        ]
        cp = _make_transcript_checkpoint("ckpt-dd00dd00", messages=messages)
        snippets = _search_checkpoint_transcript(cp, "issue 898")
        assert snippets == []

    def test_content_summary_fallback(self):
        """Falls back to content_summary when content is None."""
        messages = [
            Message(
                role=MessageRole.ASSISTANT,
                content=None,
                content_summary="Worked on issue 898 auth fix",
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            ),
        ]
        cp = _make_transcript_checkpoint("ckpt-ee00ee00", messages=messages)
        snippets = _search_checkpoint_transcript(cp, "issue 898")
        assert len(snippets) == 1
        assert "issue 898" in snippets[0]

    def test_multiple_matches(self):
        """Returns multiple snippets when text appears in multiple messages."""
        messages = [
            Message(
                role=MessageRole.USER,
                content="Fix issue 898",
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            ),
            Message(
                role=MessageRole.ASSISTANT,
                content="Working on issue 898 now",
                timestamp=datetime(2026, 1, 15, 12, 1, 0, tzinfo=UTC),
            ),
        ]
        cp = _make_transcript_checkpoint("ckpt-ff00ff00", messages=messages)
        snippets = _search_checkpoint_transcript(cp, "issue 898")
        assert len(snippets) == 2


class TestCmdSearch:
    """Integration tests for cmd_search."""

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_search_finds_matching_checkpoint(
        self, mock_ref, mock_index, mock_filter, mock_load, _mock_gw, capsys
    ):
        """search command finds and displays matching checkpoints."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = CheckpointIndexV2(
            schemaVersion="2.0",
            checkpoints=[],
            last_updated=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )

        summaries = [_make_summary("ckpt-aaa11111")]
        mock_filter.return_value = summaries

        messages = [
            Message(
                role=MessageRole.USER,
                content="Please fix issue 898",
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            ),
        ]
        checkpoint = _make_transcript_checkpoint("ckpt-aaa11111", messages=messages)
        mock_load.return_value = checkpoint

        parser = create_parser()
        args = parser.parse_args(["search", "--text", "issue 898"])
        result = cmd_search(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "issue 898" in output
        assert "1 checkpoints matched" in output

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_search_no_matches(
        self, mock_ref, mock_index, mock_filter, mock_load, _mock_gw, capsys
    ):
        """search command reports when no transcripts match."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = CheckpointIndexV2(
            schemaVersion="2.0",
            checkpoints=[],
            last_updated=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )

        summaries = [_make_summary("ckpt-bbb22222")]
        mock_filter.return_value = summaries

        messages = [
            Message(
                role=MessageRole.USER,
                content="Something completely different",
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            ),
        ]
        checkpoint = _make_transcript_checkpoint("ckpt-bbb22222", messages=messages)
        mock_load.return_value = checkpoint

        parser = create_parser()
        args = parser.parse_args(["search", "--text", "nonexistent"])
        result = cmd_search(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "No checkpoints found with transcript matching" in output

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_search_no_checkpoint_branch(self, mock_ref, _mock_gw, capsys):
        """search returns 0 with message when no checkpoint branch exists."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["search", "--text", "test"])
        result = cmd_search(args)

        assert result == 0
        output = capsys.readouterr().out
        assert "No checkpoints found" in output

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_search_json_output(
        self, mock_ref, mock_index, mock_filter, mock_load, _mock_gw, capsys
    ):
        """search --json outputs structured JSON with matching snippets."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = CheckpointIndexV2(
            schemaVersion="2.0",
            checkpoints=[],
            last_updated=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
        )

        summaries = [_make_summary("ckpt-ccc33333")]
        mock_filter.return_value = summaries

        messages = [
            Message(
                role=MessageRole.USER,
                content="Working on issue 898",
                timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
            ),
        ]
        checkpoint = _make_transcript_checkpoint("ckpt-ccc33333", messages=messages)
        mock_load.return_value = checkpoint

        parser = create_parser()
        args = parser.parse_args(["search", "--text", "issue 898", "--json"])
        result = cmd_search(args)

        assert result == 0
        output = capsys.readouterr().out
        data = json.loads(output)
        assert len(data) == 1
        assert data[0]["id"] == "ckpt-ccc33333"
        assert "matching_snippets" in data[0]
        assert len(data[0]["matching_snippets"]) > 0

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    def test_search_via_main(self, _mock_gw):
        """search subcommand is reachable via main()."""
        with patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref") as mock_ref:
            mock_ref.return_value = None
            result = main(["search", "--text", "test"])
            assert result == 0

    @patch("egg_contracts.checkpoint_cli._get_checkpoint_repo_from_args", return_value=(None, None))
    @patch("egg_contracts.checkpoint_cli._http_get")
    def test_search_http_finds_matching_checkpoint(self, mock_http, _mock_repo, capsys):
        """_cmd_search_http finds and displays matching checkpoints via HTTP."""
        # First call: list checkpoints
        mock_http.side_effect = [
            {"data": {"checkpoints": [{"id": "ckpt-http1111", "session_id": "s1"}]}},
            # Second call: show checkpoint detail with transcript
            {
                "data": {
                    "checkpoint": {
                        "id": "ckpt-http1111",
                        "transcript": {
                            "messages": [
                                {"role": "user", "content": "Fix issue 898 in the auth module"},
                            ]
                        },
                    }
                }
            },
        ]

        parser = create_parser()
        args = parser.parse_args(["search", "--text", "issue 898"])
        result = _cmd_search_http(args, _TEST_GATEWAY_URL)

        assert result == 0
        output = capsys.readouterr().out
        assert "issue 898" in output
        assert "1 checkpoints matched" in output

    @patch("egg_contracts.checkpoint_cli._get_checkpoint_repo_from_args", return_value=(None, None))
    @patch("egg_contracts.checkpoint_cli._http_get")
    def test_search_http_no_matches(self, mock_http, _mock_repo, capsys):
        """_cmd_search_http reports when no transcripts match."""
        mock_http.side_effect = [
            {"data": {"checkpoints": [{"id": "ckpt-http2222", "session_id": "s1"}]}},
            {
                "data": {
                    "checkpoint": {
                        "id": "ckpt-http2222",
                        "transcript": {
                            "messages": [
                                {"role": "user", "content": "Something unrelated"},
                            ]
                        },
                    }
                }
            },
        ]

        parser = create_parser()
        args = parser.parse_args(["search", "--text", "nonexistent"])
        result = _cmd_search_http(args, _TEST_GATEWAY_URL)

        assert result == 0
        output = capsys.readouterr().out
        assert "No checkpoints found with transcript matching" in output
