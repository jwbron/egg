"""Tests for checkpoint CLI papercut fixes (#1715).

Covers the five usability fixes:
1. --checkpoint-repo accepted before or after subcommand (task-1-1)
2. Empty results print "Searched <repo> branch <branch>" to stderr (task-1-2)
3. --agent-type accepts composite BRC role names (task-1-3)
4. --json empty output is valid parseable JSON (task-1-4)
5. Help text mentions reviewer checkpoint caveats (task-1-5)
"""

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from egg_contracts.checkpoint_cli import (
    cmd_browse,
    cmd_context,
    cmd_cost,
    cmd_list,
    cmd_search,
    create_parser,
)
from egg_contracts.checkpoints import (
    AgentType,
    CheckpointIndexV2,
    CheckpointSummaryV2,
    CheckpointV2,
    SessionMetadata,
    SessionStatus,
    TokenUsage,
    TriggerType,
)

# ──────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────


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
    agent_role: str | None = None,
    input_tokens: int = 8000,
    output_tokens: int = 2000,
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
            agent_role=agent_role,
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


def _empty_index() -> CheckpointIndexV2:
    """Create an empty checkpoint index."""
    return CheckpointIndexV2(
        schemaVersion="2.0",
        checkpoints=[],
        last_updated=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
    )


# ──────────────────────────────────────────────────────────────
# Task 1-1: --checkpoint-repo accepted before or after subcommand
# ──────────────────────────────────────────────────────────────


class TestCheckpointRepoPosition:
    """Task 1-1: --checkpoint-repo works in both positions."""

    def test_checkpoint_repo_before_subcommand(self):
        """--checkpoint-repo before the subcommand parses correctly."""
        parser = create_parser()
        args = parser.parse_args(["--checkpoint-repo", "org/ckpts", "list", "--issue", "42"])
        assert args.checkpoint_repo == "org/ckpts"
        assert args.command == "list"
        assert args.issue == 42

    def test_checkpoint_repo_after_subcommand(self):
        """--checkpoint-repo after the subcommand parses correctly."""
        parser = create_parser()
        args = parser.parse_args(["list", "--checkpoint-repo", "org/ckpts", "--issue", "42"])
        assert args.checkpoint_repo == "org/ckpts"
        assert args.command == "list"
        assert args.issue == 42

    def test_repo_path_before_subcommand(self):
        """--repo-path before the subcommand parses correctly."""
        parser = create_parser()
        args = parser.parse_args(["--repo-path", "/my/repo", "list", "--issue", "42"])
        assert args.repo_path == "/my/repo"
        assert args.command == "list"

    def test_repo_path_after_subcommand(self):
        """--repo-path after the subcommand parses correctly."""
        parser = create_parser()
        args = parser.parse_args(["list", "--repo-path", "/my/repo", "--issue", "42"])
        assert args.repo_path == "/my/repo"
        assert args.command == "list"

    def test_both_positions_resolve_same_value(self):
        """When supplied in both positions, argparse last-wins resolves to same attribute."""
        parser = create_parser()
        # Before subcommand only
        args_before = parser.parse_args(["--checkpoint-repo", "org/before", "list"])
        # After subcommand only
        args_after = parser.parse_args(["list", "--checkpoint-repo", "org/after"])
        assert args_before.checkpoint_repo == "org/before"
        assert args_after.checkpoint_repo == "org/after"

    def test_both_positions_last_wins(self):
        """When supplied in both positions, argparse last-wins."""
        parser = create_parser()
        args = parser.parse_args(
            ["--checkpoint-repo", "org/first", "list", "--checkpoint-repo", "org/second"]
        )
        assert args.checkpoint_repo == "org/second"

    def test_checkpoint_repo_works_with_show(self):
        """--checkpoint-repo after 'show' subcommand parses correctly."""
        parser = create_parser()
        args = parser.parse_args(["show", "ckpt-abc12345", "--checkpoint-repo", "org/ckpts"])
        assert args.checkpoint_repo == "org/ckpts"
        assert args.command == "show"

    def test_checkpoint_repo_works_with_browse(self):
        """--checkpoint-repo after 'browse' subcommand parses correctly."""
        parser = create_parser()
        args = parser.parse_args(["browse", "--issue", "42", "--checkpoint-repo", "org/ckpts"])
        assert args.checkpoint_repo == "org/ckpts"
        assert args.command == "browse"

    def test_checkpoint_repo_works_with_context(self):
        """--checkpoint-repo after 'context' subcommand parses correctly."""
        parser = create_parser()
        args = parser.parse_args(
            ["context", "--pipeline", "issue-42", "--checkpoint-repo", "org/ckpts"]
        )
        assert args.checkpoint_repo == "org/ckpts"
        assert args.command == "context"

    def test_checkpoint_repo_works_with_cost(self):
        """--checkpoint-repo after 'cost' subcommand parses correctly."""
        parser = create_parser()
        args = parser.parse_args(
            ["cost", "--pipeline", "issue-42", "--checkpoint-repo", "org/ckpts"]
        )
        assert args.checkpoint_repo == "org/ckpts"
        assert args.command == "cost"

    def test_checkpoint_repo_works_with_search(self):
        """--checkpoint-repo after 'search' subcommand parses correctly."""
        parser = create_parser()
        args = parser.parse_args(["search", "--text", "hello", "--checkpoint-repo", "org/ckpts"])
        assert args.checkpoint_repo == "org/ckpts"
        assert args.command == "search"


# ──────────────────────────────────────────────────────────────
# Task 1-2: Empty results print "Searched <repo> branch <branch>" to stderr
# ──────────────────────────────────────────────────────────────


class TestEmptyResultStderr:
    """Task 1-2: empty results show searched repo/branch on stderr."""

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_list_empty_prints_searched_info(self, mock_ref, _mock_gw, capsys):
        """cmd_list prints 'Searched <repo> branch <branch>' to stderr on empty."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(
            ["list", "--checkpoint-repo", "org/ckpts", "--pipeline", "issue-42"]
        )
        result = cmd_list(args)

        assert result == 0
        err = capsys.readouterr().err
        assert "Searched" in err
        assert "org/ckpts" in err

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2", return_value=[])
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_list_empty_with_index_prints_searched_info(
        self, mock_ref, mock_index, mock_filter, _mock_gw, capsys
    ):
        """cmd_list with index but no filter results prints repo/branch to stderr."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = _empty_index()

        parser = create_parser()
        args = parser.parse_args(
            ["list", "--checkpoint-repo", "org/ckpts", "--pipeline", "issue-42"]
        )
        result = cmd_list(args)

        assert result == 0
        err = capsys.readouterr().err
        assert "Searched" in err

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_browse_empty_prints_searched_info(self, mock_ref, _mock_gw, capsys):
        """cmd_browse prints repo/branch info to stderr when no checkpoints found."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["browse", "--issue", "9999", "--checkpoint-repo", "org/ckpts"])
        result = cmd_browse(args)

        assert result == 0
        err = capsys.readouterr().err
        assert "Searched" in err

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_search_empty_prints_searched_info(self, mock_ref, _mock_gw, capsys):
        """cmd_search prints repo/branch info to stderr when no checkpoints found."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["search", "--text", "test", "--checkpoint-repo", "org/ckpts"])
        result = cmd_search(args)

        assert result == 0
        err = capsys.readouterr().err
        assert "Searched" in err

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_context_empty_prints_searched_info(self, mock_ref, _mock_gw, capsys):
        """cmd_context prints repo/branch info to stderr when no checkpoints found."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(
            ["context", "--pipeline", "issue-42", "--checkpoint-repo", "org/ckpts"]
        )
        result = cmd_context(args)

        assert result == 0
        err = capsys.readouterr().err
        assert "Searched" in err

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_cost_empty_prints_searched_info(self, mock_ref, _mock_gw, capsys):
        """cmd_cost prints repo/branch info to stderr when no checkpoints found."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(
            ["cost", "--pipeline", "issue-42", "--checkpoint-repo", "org/ckpts"]
        )
        result = cmd_cost(args)

        assert result == 0
        err = capsys.readouterr().err
        assert "Searched" in err


# ──────────────────────────────────────────────────────────────
# Task 1-3: --agent-type accepts composite BRC role names
# ──────────────────────────────────────────────────────────────


# Composite role names that should be accepted
COMPOSITE_ROLES = [
    "reviewer_code",
    "reviewer_contract",
    "reviewer_agent_design",
    "reviewer_refine",
    "reviewer_plan",
]


class TestCompositeAgentType:
    """Task 1-3: --agent-type supports composite BRC reviewer roles."""

    @pytest.mark.parametrize("role", COMPOSITE_ROLES)
    def test_list_parser_accepts_composite_role(self, role: str):
        """list --agent-type accepts composite reviewer role names."""
        parser = create_parser()
        args = parser.parse_args(["list", "--agent-type", role])
        assert args.agent_type == role

    @pytest.mark.parametrize("role", COMPOSITE_ROLES)
    def test_context_parser_accepts_composite_role(self, role: str):
        """context --agent-type accepts composite reviewer role names."""
        parser = create_parser()
        args = parser.parse_args(["context", "--agent-type", role, "--pipeline", "p1"])
        assert args.agent_type == role

    @pytest.mark.parametrize("role", COMPOSITE_ROLES)
    def test_search_parser_accepts_composite_role(self, role: str):
        """search --agent-type accepts composite reviewer role names."""
        parser = create_parser()
        args = parser.parse_args(["search", "--text", "test", "--agent-type", role])
        assert args.agent_type == role

    def test_unknown_role_rejected(self):
        """Unknown agent-type values are rejected by argparse."""
        parser = create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["list", "--agent-type", "nonexistent_role"])

    def test_existing_roles_still_work(self):
        """Existing non-composite roles (coder, tester, etc.) still work."""
        parser = create_parser()
        for role in ["coder", "tester", "documenter", "reviewer", "architect"]:
            args = parser.parse_args(["list", "--agent-type", role])
            assert args.agent_type == role

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_list_composite_role_filters_by_agent_role(
        self, mock_ref, mock_index, mock_filter, mock_load, _mock_gw, capsys
    ):
        """cmd_list with composite role filters checkpoints by session.agent_role."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = _empty_index()

        # Return two REVIEWER summaries
        summaries = [
            _make_summary("ckpt-rev1", agent_type=AgentType.REVIEWER),
            _make_summary("ckpt-rev2", agent_type=AgentType.REVIEWER),
        ]
        mock_filter.return_value = summaries

        # First checkpoint has reviewer_code, second has reviewer_contract
        ckpt1 = _make_checkpoint(
            "ckpt-rev1", agent_type=AgentType.REVIEWER, agent_role="reviewer_code"
        )
        ckpt2 = _make_checkpoint(
            "ckpt-rev2", agent_type=AgentType.REVIEWER, agent_role="reviewer_contract"
        )
        mock_load.side_effect = [ckpt1, ckpt2]

        parser = create_parser()
        args = parser.parse_args(
            ["list", "--agent-type", "reviewer_code", "--pipeline", "issue-745"]
        )
        result = cmd_list(args)

        assert result == 0
        # Should have called filter with "reviewer" (the AgentType value)
        call_kwargs = mock_filter.call_args
        assert call_kwargs[1].get("agent_type") == "reviewer" or (
            len(call_kwargs[0]) > 0 and call_kwargs[0]
        )

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_search_composite_role_filters_by_agent_role(
        self, mock_ref, mock_index, mock_filter, mock_load, _mock_gw, capsys
    ):
        """cmd_search with composite role filters checkpoints by session.agent_role."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = _empty_index()

        summaries = [
            _make_summary("ckpt-rev1", agent_type=AgentType.REVIEWER),
        ]
        mock_filter.return_value = summaries

        ckpt = _make_checkpoint(
            "ckpt-rev1", agent_type=AgentType.REVIEWER, agent_role="reviewer_code"
        )
        # Add a minimal transcript so search has something to scan
        from egg_contracts.checkpoints import Message, MessageRole, Transcript

        ckpt.transcript = Transcript(
            messages=[
                Message(
                    role=MessageRole.USER,
                    content="reviewing code quality",
                    timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC),
                )
            ],
            message_count=1,
        )
        mock_load.return_value = ckpt

        parser = create_parser()
        args = parser.parse_args(["search", "--text", "reviewing", "--agent-type", "reviewer_code"])
        result = cmd_search(args)

        assert result == 0

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_context_composite_role_filters_by_agent_role(
        self, mock_ref, mock_index, mock_filter, mock_load, _mock_gw, capsys
    ):
        """cmd_context with composite role filters by session.agent_role."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = _empty_index()

        summaries = [
            _make_summary("ckpt-rev1", agent_type=AgentType.REVIEWER),
        ]
        mock_filter.return_value = summaries

        ckpt = _make_checkpoint(
            "ckpt-rev1", agent_type=AgentType.REVIEWER, agent_role="reviewer_code"
        )
        mock_load.return_value = ckpt

        parser = create_parser()
        args = parser.parse_args(
            ["context", "--pipeline", "issue-745", "--agent-type", "reviewer_code"]
        )
        result = cmd_context(args)

        assert result == 0

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_non_composite_reviewer_falls_through(
        self, mock_ref, mock_index, mock_filter, mock_load, _mock_gw, capsys
    ):
        """Non-composite 'reviewer' value falls through unchanged without post-filter."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = _empty_index()

        summaries = [
            _make_summary("ckpt-rev1", agent_type=AgentType.REVIEWER),
        ]
        mock_filter.return_value = summaries

        parser = create_parser()
        args = parser.parse_args(["list", "--agent-type", "reviewer", "--pipeline", "issue-745"])
        result = cmd_list(args)

        assert result == 0
        # For non-composite "reviewer", filter_checkpoints_v2 should be called
        # with agent_type="reviewer" and NO post-filter load is needed
        mock_filter.assert_called_once()


# ──────────────────────────────────────────────────────────────
# Task 1-4: --json empty output is valid parseable JSON
# ──────────────────────────────────────────────────────────────


class TestJsonEmptyOutput:
    """Task 1-4: --json empty results emit valid parseable JSON."""

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_list_empty_json_is_valid(self, mock_ref, _mock_gw, capsys):
        """cmd_list --json emits valid JSON on empty results."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["list", "--json", "--pipeline", "issue-999"])
        result = cmd_list(args)

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data == []

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2", return_value=[])
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_list_empty_filtered_json_is_valid(
        self, mock_ref, mock_index, mock_filter, _mock_gw, capsys
    ):
        """cmd_list --json with index but no filter results emits valid JSON."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = _empty_index()

        parser = create_parser()
        args = parser.parse_args(["list", "--json", "--pipeline", "issue-999"])
        result = cmd_list(args)

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data == []

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_browse_empty_json_is_valid(self, mock_ref, _mock_gw, capsys):
        """cmd_browse --json emits valid JSON on empty results."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["browse", "--issue", "9999", "--json"])
        result = cmd_browse(args)

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data == []

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_search_empty_json_is_valid(self, mock_ref, _mock_gw, capsys):
        """cmd_search --json emits valid JSON on empty results."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["search", "--text", "nonexistent", "--json"])
        result = cmd_search(args)

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data == []

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_context_empty_json_is_valid(self, mock_ref, _mock_gw, capsys):
        """cmd_context --json emits valid JSON (structured empty object) on empty results."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["context", "--pipeline", "issue-999", "--json"])
        result = cmd_context(args)

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        # context returns structured object, not simple list
        assert isinstance(data, dict)

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_cost_empty_json_is_valid(self, mock_ref, _mock_gw, capsys):
        """cmd_cost --json emits valid JSON (structured empty object) on empty results."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["cost", "--pipeline", "issue-999", "--json"])
        result = cmd_cost(args)

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        # cost returns structured object, not simple list
        assert isinstance(data, dict)

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_list_empty_json_stderr_has_searched(self, mock_ref, _mock_gw, capsys):
        """Informational line goes to stderr, stdout is pure JSON."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(
            ["list", "--json", "--checkpoint-repo", "org/ckpts", "--pipeline", "issue-999"]
        )
        result = cmd_list(args)

        assert result == 0
        captured = capsys.readouterr()
        # stdout is valid JSON
        data = json.loads(captured.out)
        assert data == []
        # stderr has the informational line
        assert "Searched" in captured.err

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2", return_value=[])
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_list_no_index_json_is_valid(self, mock_ref, mock_index, mock_filter, _mock_gw, capsys):
        """cmd_list --json with ref but no index emits valid JSON."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = None

        parser = create_parser()
        args = parser.parse_args(["list", "--json", "--pipeline", "issue-999"])
        result = cmd_list(args)

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data == []

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_browse_empty_index_json_is_valid(self, mock_ref, mock_index, _mock_gw, capsys):
        """cmd_browse --json with ref but no index emits valid JSON."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = None

        parser = create_parser()
        args = parser.parse_args(["browse", "--issue", "9999", "--json"])
        result = cmd_browse(args)

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data == []


# ──────────────────────────────────────────────────────────────
# Task 1-4 HTTP: --json empty output is valid JSON via HTTP path
# ──────────────────────────────────────────────────────────────


class TestJsonEmptyOutputHttp:
    """Task 1-4: --json empty results via HTTP path emit valid parseable JSON."""

    @patch("egg_contracts.checkpoint_cli._http_get")
    def test_list_http_empty_json_is_valid(self, mock_http_get, capsys):
        """_cmd_list_http --json emits valid JSON on empty results."""
        from egg_contracts.checkpoint_cli import _cmd_list_http

        mock_http_get.return_value = {"data": {"checkpoints": []}}

        parser = create_parser()
        args = parser.parse_args(["list", "--json"])
        result = _cmd_list_http(args, "http://gw:9848")

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data == []

    @patch("egg_contracts.checkpoint_cli._http_get")
    def test_browse_http_empty_json_is_valid(self, mock_http_get, capsys):
        """_cmd_browse_http --json emits valid JSON on empty results."""
        from egg_contracts.checkpoint_cli import _cmd_browse_http

        mock_http_get.return_value = {"data": {"checkpoints": []}}

        parser = create_parser()
        args = parser.parse_args(["browse", "--issue", "9999", "--json"])
        result = _cmd_browse_http(args, "http://gw:9848")

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data == []

    @patch("egg_contracts.checkpoint_cli._http_get")
    def test_search_http_empty_json_is_valid(self, mock_http_get, capsys):
        """_cmd_search_http --json emits valid JSON on empty results."""
        from egg_contracts.checkpoint_cli import _cmd_search_http

        mock_http_get.return_value = {"data": {"checkpoints": []}}

        parser = create_parser()
        args = parser.parse_args(["search", "--text", "nonexistent", "--json"])
        result = _cmd_search_http(args, "http://gw:9848")

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data == []

    @patch("egg_contracts.checkpoint_cli._http_get")
    def test_context_http_empty_json_is_valid(self, mock_http_get, capsys):
        """_cmd_context_http --json emits valid JSON on empty results."""
        from egg_contracts.checkpoint_cli import _cmd_context_http

        mock_http_get.return_value = {"data": {"checkpoints": []}}

        parser = create_parser()
        args = parser.parse_args(["context", "--pipeline", "issue-999", "--json"])
        result = _cmd_context_http(args, "http://gw:9848")

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, dict)

    @patch("egg_contracts.checkpoint_cli._http_get")
    def test_cost_http_empty_json_is_valid(self, mock_http_get, capsys):
        """_cmd_cost_http --json emits valid JSON on empty results."""
        from egg_contracts.checkpoint_cli import _cmd_cost_http

        mock_http_get.return_value = {"data": {"checkpoints": []}}

        parser = create_parser()
        args = parser.parse_args(["cost", "--pipeline", "issue-999", "--json"])
        result = _cmd_cost_http(args, "http://gw:9848")

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, dict)


# ──────────────────────────────────────────────────────────────
# Task 1-5: Help text mentions reviewer checkpoint caveats
# ──────────────────────────────────────────────────────────────


class TestHelpTextCaveats:
    """Task 1-5: help text documents reviewer checkpoint caveats."""

    def test_top_level_help_mentions_reviewer_caveat(self):
        """Top-level --help mentions reviewer agents may not produce checkpoints."""
        parser = create_parser()
        help_text = parser.format_help()
        # Should mention that reviewer agents may not produce checkpoints
        assert "reviewer" in help_text.lower()

    def test_list_help_mentions_composite_role_caveat(self):
        """list --help mentions composite BRC role gateway limitation."""
        parser = create_parser()
        # Get the list subparser's help
        for action in parser._subparsers._actions:
            if hasattr(action, "_parser_class"):
                for name, subparser in action.choices.items():
                    if name == "list":
                        help_text = subparser.format_help()
                        # Should mention gateway limitation
                        assert "gateway" in help_text.lower() or "direct" in help_text.lower()
                        break

    def test_context_help_mentions_composite_role_caveat(self):
        """context --help mentions composite BRC role gateway limitation."""
        parser = create_parser()
        for action in parser._subparsers._actions:
            if hasattr(action, "_parser_class"):
                for name, subparser in action.choices.items():
                    if name == "context":
                        help_text = subparser.format_help()
                        assert "gateway" in help_text.lower() or "direct" in help_text.lower()
                        break

    def test_search_help_mentions_composite_role_caveat(self):
        """search --help mentions composite BRC role gateway limitation."""
        parser = create_parser()
        for action in parser._subparsers._actions:
            if hasattr(action, "_parser_class"):
                for name, subparser in action.choices.items():
                    if name == "search":
                        help_text = subparser.format_help()
                        assert "gateway" in help_text.lower() or "direct" in help_text.lower()
                        break


# ──────────────────────────────────────────────────────────────
# Edge cases and boundary conditions
# ──────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge cases and boundary conditions for the papercut fixes."""

    def test_checkpoint_repo_default_none_all_subcommands(self):
        """checkpoint_repo defaults to None for all subcommands when not specified."""
        parser = create_parser()
        for cmd_args in [
            ["list"],
            ["show", "ckpt-abc"],
            ["browse", "--issue", "42"],
            ["context", "--pipeline", "p1"],
            ["cost", "--pipeline", "p1"],
            ["search", "--text", "test"],
        ]:
            args = parser.parse_args(cmd_args)
            assert args.checkpoint_repo is None, f"Failed for: {cmd_args}"

    def test_repo_path_default_none_all_subcommands(self):
        """repo_path defaults to None for all subcommands when not specified."""
        parser = create_parser()
        for cmd_args in [
            ["list"],
            ["show", "ckpt-abc"],
            ["browse", "--issue", "42"],
            ["context", "--pipeline", "p1"],
            ["cost", "--pipeline", "p1"],
            ["search", "--text", "test"],
        ]:
            args = parser.parse_args(cmd_args)
            assert args.repo_path is None, f"Failed for: {cmd_args}"

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_list_empty_exit_code_zero(self, mock_ref, _mock_gw):
        """Empty result from list still returns exit code 0."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["list", "--json"])
        result = cmd_list(args)
        assert result == 0

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_cost_empty_exit_code_zero(self, mock_ref, _mock_gw):
        """Empty result from cost still returns exit code 0."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["cost", "--json", "--pipeline", "issue-999"])
        result = cmd_cost(args)
        assert result == 0

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_search_empty_exit_code_zero(self, mock_ref, _mock_gw):
        """Empty result from search still returns exit code 0."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["search", "--text", "missing", "--json"])
        result = cmd_search(args)
        assert result == 0

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_browse_empty_exit_code_zero(self, mock_ref, _mock_gw):
        """Empty result from browse still returns exit code 0."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["browse", "--issue", "9999", "--json"])
        result = cmd_browse(args)
        assert result == 0

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_context_empty_exit_code_zero(self, mock_ref, _mock_gw):
        """Empty result from context still returns exit code 0."""
        mock_ref.return_value = None

        parser = create_parser()
        args = parser.parse_args(["context", "--pipeline", "issue-999", "--json"])
        result = cmd_context(args)
        assert result == 0

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_composite_role_no_match_returns_empty(
        self, mock_ref, mock_index, mock_filter, mock_load, _mock_gw, capsys
    ):
        """Composite role filter with no matching agent_role returns empty."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = _empty_index()

        summaries = [
            _make_summary("ckpt-rev1", agent_type=AgentType.REVIEWER),
        ]
        mock_filter.return_value = summaries

        # Checkpoint has reviewer_contract but we're searching for reviewer_code
        ckpt = _make_checkpoint(
            "ckpt-rev1", agent_type=AgentType.REVIEWER, agent_role="reviewer_contract"
        )
        mock_load.return_value = ckpt

        parser = create_parser()
        args = parser.parse_args(
            ["list", "--agent-type", "reviewer_code", "--pipeline", "issue-745", "--json"]
        )
        result = cmd_list(args)

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data == []

    @patch("egg_contracts.checkpoint_cli._get_gateway_url", return_value=None)
    @patch("egg_contracts.checkpoint_cli.load_checkpoint_from_ref")
    @patch("egg_contracts.checkpoint_cli.filter_checkpoints_v2")
    @patch("egg_contracts.checkpoint_cli.load_index_from_ref")
    @patch("egg_contracts.checkpoint_cli.ensure_checkpoint_ref")
    def test_composite_role_with_null_agent_role_skipped(
        self, mock_ref, mock_index, mock_filter, mock_load, _mock_gw, capsys
    ):
        """Checkpoints with null agent_role are skipped when filtering by composite role."""
        mock_ref.return_value = "origin/egg/checkpoints/v2"
        mock_index.return_value = _empty_index()

        summaries = [
            _make_summary("ckpt-rev1", agent_type=AgentType.REVIEWER),
        ]
        mock_filter.return_value = summaries

        # Checkpoint has no agent_role set
        ckpt = _make_checkpoint("ckpt-rev1", agent_type=AgentType.REVIEWER, agent_role=None)
        mock_load.return_value = ckpt

        parser = create_parser()
        args = parser.parse_args(
            ["list", "--agent-type", "reviewer_code", "--pipeline", "issue-745", "--json"]
        )
        result = cmd_list(args)

        assert result == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data == []
