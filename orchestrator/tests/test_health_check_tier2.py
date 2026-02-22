"""Comprehensive tests for Tier 2 health checks (agent inspector).

Covers:
- Context assembly: contract loading, truncation, lazy evaluation
- Prompt construction: all fields present, caps respected
- Verdict parsing: valid JSON, malformed JSON, missing fields, code fences
- AgentInspectorCheck: healthy/degraded/failed verdicts, API timeout,
  API errors, graceful degradation, event emission
- Escalation logic: runner integration with Tier 2
"""

import json
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

from health_checks.context import _TIER2_CHAR_CAP, PipelineHealthContext, _truncate
from health_checks.runner import HealthCheckRunner
from health_checks.tier2.agent_inspector import (
    AgentInspectorCheck,
    _build_user_prompt,
    _call_claude_api,
    _parse_verdict,
)
from health_checks.types import (
    HealthAction,
    HealthCheck,
    HealthResult,
    HealthStatus,
    HealthTier,
    HealthTrigger,
)
from models import (
    Pipeline,
    PipelinePhase,
    PipelineStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    status: PipelineStatus = PipelineStatus.RUNNING,
    phase: PipelinePhase = PipelinePhase.IMPLEMENT,
    issue_number: int = 99,
) -> Pipeline:
    return Pipeline(
        id="issue-99",
        issue_number=issue_number,
        repo="owner/repo",
        branch="egg/issue-99",
        mode="issue",
        status=status,
        current_phase=phase,
    )


def _make_context(
    pipeline: Pipeline | None = None,
    repo_path: Path | None = None,
    trigger: str = "phase_complete",
) -> PipelineHealthContext:
    if pipeline is None:
        pipeline = _make_pipeline()
    if repo_path is None:
        repo_path = Path("/tmp/fake-repos")
    return PipelineHealthContext(
        pipeline=pipeline,
        repo_path=repo_path,
        trigger=trigger,
    )


def _mock_httpx_response(status_code: int = 200, json_body: dict | None = None):
    """Create a mock httpx Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        import httpx

        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    resp.json.return_value = json_body or {}
    return resp


# ===========================================================================
# 1. Context Assembly Tests
# ===========================================================================


class TestContextContract:
    """Tests for the contract lazy property on PipelineHealthContext."""

    def test_contract_loaded_from_file(self, tmp_path):
        """Contract JSON is loaded when issue number matches a file."""
        state_dir = tmp_path / ".egg-state" / "contracts"
        state_dir.mkdir(parents=True)
        contract_data = {
            "schemaVersion": "1.0",
            "current_phase": "implement",
            "acceptance_criteria": ["Tests pass"],
        }
        (state_dir / "99.json").write_text(json.dumps(contract_data))

        pipeline = _make_pipeline(issue_number=99)
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert ctx.contract == contract_data

    def test_contract_empty_when_no_file(self, tmp_path):
        """Returns empty dict when contract file doesn't exist."""
        ctx = PipelineHealthContext(
            pipeline=_make_pipeline(),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert ctx.contract == {}

    def test_contract_empty_when_no_issue_number(self, tmp_path):
        """Returns empty dict when pipeline has no issue number."""
        pipeline = Pipeline(
            id="test",
            issue_number=None,
            repo="owner/repo",
            branch="egg/test",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert ctx.contract == {}

    def test_contract_empty_on_invalid_json(self, tmp_path):
        """Returns empty dict when contract file has invalid JSON."""
        state_dir = tmp_path / ".egg-state" / "contracts"
        state_dir.mkdir(parents=True)
        (state_dir / "99.json").write_text("not valid json {{{")

        ctx = PipelineHealthContext(
            pipeline=_make_pipeline(),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert ctx.contract == {}

    def test_contract_is_lazy_and_cached(self, tmp_path):
        """Contract is only read once (cached on first access)."""
        state_dir = tmp_path / ".egg-state" / "contracts"
        state_dir.mkdir(parents=True)
        (state_dir / "99.json").write_text('{"phase": "implement"}')

        ctx = PipelineHealthContext(
            pipeline=_make_pipeline(),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        # First access triggers read
        result1 = ctx.contract
        # Second access returns cached value
        result2 = ctx.contract
        assert result1 is result2
        assert result1 == {"phase": "implement"}

    def test_contract_truncated_for_large_files(self, tmp_path):
        """Contract content is capped at _TIER2_CHAR_CAP chars before parsing."""
        state_dir = tmp_path / ".egg-state" / "contracts"
        state_dir.mkdir(parents=True)
        # Write a huge file that's valid JSON only at the start
        huge_content = '{"key": "' + "x" * (_TIER2_CHAR_CAP + 5000) + '"}'
        (state_dir / "99.json").write_text(huge_content)

        ctx = PipelineHealthContext(
            pipeline=_make_pipeline(),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        # Should return empty dict since truncation makes JSON invalid
        assert ctx.contract == {}

    def test_contract_resolves_via_repo_name(self, tmp_path):
        """Contract resolves through repo_path/repo_name/.egg-state/."""
        repo_dir = tmp_path / "repo"
        state_dir = repo_dir / ".egg-state" / "contracts"
        state_dir.mkdir(parents=True)
        (state_dir / "99.json").write_text('{"found": true}')

        ctx = PipelineHealthContext(
            pipeline=_make_pipeline(),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert ctx.contract == {"found": True}


class TestContextTruncation:
    """Tests for the _truncate helper and diff stat truncation."""

    def test_truncate_short_text_unchanged(self):
        assert _truncate("hello", 100) == "hello"

    def test_truncate_long_text_capped(self):
        text = "a" * 200
        result = _truncate(text, 50)
        assert len(result) == 50 + len("\n... [truncated]")
        assert result.endswith("\n... [truncated]")
        assert result.startswith("a" * 50)

    def test_truncate_exact_boundary(self):
        text = "a" * 100
        assert _truncate(text, 100) == text

    def test_git_diff_stat_truncated(self, tmp_path):
        """git_diff_stat applies truncation."""
        ctx = _make_context(repo_path=tmp_path)
        huge_diff = "x" * (_TIER2_CHAR_CAP + 1000)
        with patch.object(ctx, "_run_git", return_value=huge_diff):
            result = ctx.git_diff_stat
            assert len(result) <= _TIER2_CHAR_CAP + 50  # allow for truncation marker
            assert "[truncated]" in result


class TestContextAgentOutputs:
    """Tests for agent_outputs reading from .egg-state/."""

    def test_reads_from_agent_outputs_subdir(self, tmp_path):
        """Agent outputs reads from agent-outputs/ in addition to drafts/ and contracts/."""
        outputs_dir = tmp_path / ".egg-state" / "agent-outputs"
        outputs_dir.mkdir(parents=True)
        (outputs_dir / "coder-output.json").write_text('{"done": true}')

        ctx = PipelineHealthContext(
            pipeline=Pipeline(
                id="test", issue_number=1, repo=None, branch="egg/test",
                mode="issue", status=PipelineStatus.RUNNING,
                current_phase=PipelinePhase.IMPLEMENT,
            ),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert "coder-output.json" in ctx.agent_outputs
        assert '{"done": true}' in ctx.agent_outputs["coder-output.json"]


# ===========================================================================
# 2. Prompt Construction Tests
# ===========================================================================


class TestBuildUserPrompt:
    """Tests for _build_user_prompt."""

    def test_includes_pipeline_metadata(self, tmp_path):
        ctx = _make_context(repo_path=tmp_path)
        prompt = _build_user_prompt(ctx)
        assert "issue-99" in prompt
        assert "implement" in prompt
        assert "egg/issue-99" in prompt

    def test_includes_git_log(self, tmp_path):
        ctx = _make_context(repo_path=tmp_path)
        with patch.object(ctx, "_run_git", return_value="abc1234 Add feature"):
            # Force lazy loading
            _ = ctx.git_log
        prompt = _build_user_prompt(ctx)
        assert "abc1234 Add feature" in prompt

    def test_includes_diff_stat(self, tmp_path):
        ctx = _make_context(repo_path=tmp_path)
        with patch.object(ctx, "_run_git", return_value="file.py | 5 ++---"):
            # Force lazy loading for diff stat
            _ = ctx.git_diff_stat
        prompt = _build_user_prompt(ctx)
        assert "file.py" in prompt

    def test_includes_agent_outputs(self, tmp_path):
        state_dir = tmp_path / ".egg-state" / "drafts"
        state_dir.mkdir(parents=True)
        (state_dir / "plan.md").write_text("# Implementation Plan\nStep 1: Do things")

        pipeline = Pipeline(
            id="issue-99", issue_number=99, repo=None, branch="egg/issue-99",
            mode="issue", status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        prompt = _build_user_prompt(ctx)
        assert "plan.md" in prompt
        assert "Implementation Plan" in prompt

    def test_includes_contract_state(self, tmp_path):
        state_dir = tmp_path / ".egg-state" / "contracts"
        state_dir.mkdir(parents=True)
        contract = {
            "current_phase": "implement",
            "acceptance_criteria": ["All tests pass"],
            "agent_executions": [{"role": "coder", "status": "complete"}],
        }
        (state_dir / "99.json").write_text(json.dumps(contract))

        pipeline = Pipeline(
            id="issue-99", issue_number=99, repo=None, branch="egg/issue-99",
            mode="issue", status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        prompt = _build_user_prompt(ctx)
        assert "Contract State" in prompt
        assert "implement" in prompt

    def test_empty_outputs_shows_none_found(self, tmp_path):
        ctx = _make_context(repo_path=tmp_path)
        prompt = _build_user_prompt(ctx)
        assert "(none found)" in prompt

    def test_output_content_capped_in_prompt(self, tmp_path):
        state_dir = tmp_path / ".egg-state" / "drafts"
        state_dir.mkdir(parents=True)
        (state_dir / "huge.md").write_text("x" * 5000)

        pipeline = Pipeline(
            id="issue-99", issue_number=99, repo=None, branch="egg/issue-99",
            mode="issue", status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        prompt = _build_user_prompt(ctx)
        # Content in prompt should be capped at 2000 chars per output
        lines_with_x = [line for line in prompt.split("\n") if "x" * 100 in line]
        for line in lines_with_x:
            assert len(line) <= 2001  # some tolerance


# ===========================================================================
# 3. Verdict Parsing Tests
# ===========================================================================


class TestParseVerdict:
    """Tests for _parse_verdict."""

    def test_healthy_verdict(self):
        text = '{"status": "HEALTHY", "reasoning": "Agent is making good progress."}'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.HEALTHY
        assert "good progress" in reasoning

    def test_degraded_verdict(self):
        text = '{"status": "DEGRADED", "reasoning": "No recent commits detected."}'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.DEGRADED
        assert "No recent commits" in reasoning

    def test_failed_verdict(self):
        text = '{"status": "FAILED", "reasoning": "Agent appears stuck in error loop."}'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.FAILED
        assert "error loop" in reasoning

    def test_json_with_markdown_fences(self):
        text = '```json\n{"status": "HEALTHY", "reasoning": "All good."}\n```'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.HEALTHY
        assert "All good" in reasoning

    def test_json_with_bare_fences(self):
        text = '```\n{"status": "DEGRADED", "reasoning": "Concerning."}\n```'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.DEGRADED

    def test_malformed_json_returns_healthy(self):
        """Malformed JSON gracefully returns HEALTHY."""
        status, reasoning = _parse_verdict("not json at all")
        assert status == HealthStatus.HEALTHY
        assert "Could not parse" in reasoning

    def test_empty_response_returns_healthy(self):
        status, reasoning = _parse_verdict("")
        assert status == HealthStatus.HEALTHY
        assert "Could not parse" in reasoning

    def test_unknown_status_defaults_to_healthy(self):
        text = '{"status": "UNKNOWN", "reasoning": "Something weird."}'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.HEALTHY
        assert "Unknown status" in reasoning

    def test_missing_status_defaults_to_healthy(self):
        text = '{"reasoning": "No status field."}'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.HEALTHY
        assert "Unknown status" in reasoning

    def test_missing_reasoning_has_default(self):
        text = '{"status": "HEALTHY"}'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.HEALTHY
        assert "No reasoning provided" in reasoning

    def test_lowercase_status_handled(self):
        """Status comparison is case-insensitive."""
        text = '{"status": "degraded", "reasoning": "Concern."}'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.DEGRADED


# ===========================================================================
# 4. Claude API Call Tests
# ===========================================================================


class TestCallClaudeApi:
    """Tests for _call_claude_api with mocked httpx."""

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_successful_api_call(self, mock_httpx):
        """Successful API call returns text content."""
        mock_resp = _mock_httpx_response(
            200,
            {"content": [{"type": "text", "text": '{"status": "HEALTHY", "reasoning": "OK"}'}]},
        )
        mock_httpx.post.return_value = mock_resp

        result = _call_claude_api("test prompt", api_key="sk-ant-test123")
        assert '"HEALTHY"' in result
        mock_httpx.post.assert_called_once()

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_api_sends_correct_headers(self, mock_httpx):
        """API call sends correct Anthropic headers."""
        mock_resp = _mock_httpx_response(200, {"content": [{"type": "text", "text": "ok"}]})
        mock_httpx.post.return_value = mock_resp

        _call_claude_api("test", api_key="sk-ant-key", base_url="https://custom.api.com")

        call_args = mock_httpx.post.call_args
        assert call_args[0][0] == "https://custom.api.com/v1/messages"
        headers = call_args[1]["headers"]
        assert headers["x-api-key"] == "sk-ant-key"
        assert headers["anthropic-version"] == "2023-06-01"

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_api_sends_correct_model(self, mock_httpx):
        """API call uses specified model."""
        mock_resp = _mock_httpx_response(200, {"content": [{"type": "text", "text": "ok"}]})
        mock_httpx.post.return_value = mock_resp

        _call_claude_api("test", api_key="sk-ant-key", model="claude-sonnet-4-20250514")

        payload = mock_httpx.post.call_args[1]["json"]
        assert payload["model"] == "claude-sonnet-4-20250514"

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_api_timeout_raises(self, mock_httpx):
        """Timeout exception propagates after retries."""
        import httpx
        import pytest

        mock_httpx.TimeoutException = httpx.TimeoutException
        mock_httpx.HTTPStatusError = httpx.HTTPStatusError
        mock_httpx.post.side_effect = httpx.TimeoutException("Connection timed out")

        with pytest.raises(httpx.TimeoutException):
            _call_claude_api("test", api_key="sk-ant-key")
        # Should have retried once (2 calls total)
        assert mock_httpx.post.call_count == 2

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_api_http_error_raises(self, mock_httpx):
        """HTTP 500 errors propagate after retries."""
        import httpx
        import pytest

        mock_httpx.TimeoutException = httpx.TimeoutException
        mock_httpx.HTTPStatusError = httpx.HTTPStatusError

        mock_resp = _mock_httpx_response(500)
        mock_httpx.post.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            _call_claude_api("test", api_key="sk-ant-key")
        # Should have retried once (2 calls total)
        assert mock_httpx.post.call_count == 2

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_api_empty_content_blocks(self, mock_httpx):
        """Empty content blocks return empty string."""
        mock_resp = _mock_httpx_response(200, {"content": []})
        mock_httpx.post.return_value = mock_resp

        result = _call_claude_api("test", api_key="sk-ant-key")
        assert result == ""

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_api_uses_env_vars(self, mock_httpx):
        """Falls back to env vars for config."""
        mock_resp = _mock_httpx_response(200, {"content": [{"type": "text", "text": "ok"}]})
        mock_httpx.post.return_value = mock_resp

        with patch.dict("os.environ", {
            "ANTHROPIC_API_KEY": "sk-ant-env-key",
            "ANTHROPIC_BASE_URL": "https://env.api.com",
            "HEALTH_CHECK_MODEL": "claude-3-haiku-20240307",
        }):
            _call_claude_api("test")

        call_args = mock_httpx.post.call_args
        assert call_args[0][0] == "https://env.api.com/v1/messages"
        assert call_args[1]["headers"]["x-api-key"] == "sk-ant-env-key"
        assert call_args[1]["json"]["model"] == "claude-3-haiku-20240307"

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_retry_then_success(self, mock_httpx):
        """First attempt fails, retry succeeds."""
        import httpx

        mock_httpx.TimeoutException = httpx.TimeoutException
        mock_httpx.HTTPStatusError = httpx.HTTPStatusError

        success_resp = _mock_httpx_response(
            200, {"content": [{"type": "text", "text": "success"}]}
        )
        mock_httpx.post.side_effect = [
            httpx.TimeoutException("timeout"),
            success_resp,
        ]

        result = _call_claude_api("test", api_key="sk-ant-key")
        assert result == "success"
        assert mock_httpx.post.call_count == 2


# ===========================================================================
# 5. AgentInspectorCheck Integration Tests
# ===========================================================================


class TestAgentInspectorCheck:
    """Tests for the full AgentInspectorCheck.run() method."""

    def test_conforms_to_health_check_protocol(self):
        """AgentInspectorCheck satisfies the HealthCheck protocol."""
        check = AgentInspectorCheck()
        assert isinstance(check, HealthCheck)
        assert check.name == "agent_inspector"
        assert check.tier == HealthTier.AGENT
        assert HealthTrigger.WAVE_COMPLETE in check.triggers
        assert HealthTrigger.PHASE_COMPLETE in check.triggers
        assert HealthTrigger.ON_DEMAND in check.triggers

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_healthy_verdict_returns_healthy(self, mock_api, tmp_path):
        mock_api.return_value = '{"status": "HEALTHY", "reasoning": "Everything looks fine."}'

        check = AgentInspectorCheck()
        ctx = _make_context(repo_path=tmp_path)
        result = check.run(ctx)

        assert result.status == HealthStatus.HEALTHY
        assert result.check_name == "agent_inspector"
        assert result.tier == HealthTier.AGENT
        assert result.action == HealthAction.CONTINUE
        assert "Everything looks fine" in result.reasoning

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_degraded_verdict_returns_alert(self, mock_api, tmp_path):
        mock_api.return_value = '{"status": "DEGRADED", "reasoning": "Stale output files."}'

        check = AgentInspectorCheck()
        result = check.run(_make_context(repo_path=tmp_path))

        assert result.status == HealthStatus.DEGRADED
        assert result.action == HealthAction.ALERT
        assert "Stale output" in result.reasoning

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_failed_verdict_returns_alert_not_fail_pipeline(self, mock_api, tmp_path):
        """FAILED verdict returns ALERT action (not FAIL_PIPELINE per design)."""
        mock_api.return_value = '{"status": "FAILED", "reasoning": "Agent is stuck."}'

        check = AgentInspectorCheck()
        result = check.run(_make_context(repo_path=tmp_path))

        assert result.status == HealthStatus.FAILED
        assert result.action == HealthAction.ALERT  # NOT FAIL_PIPELINE
        assert "stuck" in result.reasoning

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_api_timeout_graceful_degradation(self, mock_api, tmp_path):
        """API timeout results in HEALTHY with warning (graceful degradation)."""
        import httpx

        mock_api.side_effect = httpx.TimeoutException("Connection timed out")

        check = AgentInspectorCheck()
        result = check.run(_make_context(repo_path=tmp_path))

        assert result.status == HealthStatus.HEALTHY
        assert result.action == HealthAction.CONTINUE
        assert "unavailable" in result.reasoning
        assert result.details.get("graceful_degradation") is True

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_api_http_error_graceful_degradation(self, mock_api, tmp_path):
        """HTTP error results in HEALTHY with warning."""
        import httpx

        mock_api.side_effect = httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=MagicMock(),
            response=MagicMock(status_code=500),
        )

        check = AgentInspectorCheck()
        result = check.run(_make_context(repo_path=tmp_path))

        assert result.status == HealthStatus.HEALTHY
        assert result.action == HealthAction.CONTINUE
        assert "unavailable" in result.reasoning
        assert "error" in result.details

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_malformed_response_graceful_healthy(self, mock_api, tmp_path):
        """Malformed API response defaults to HEALTHY."""
        mock_api.return_value = "This is not JSON at all"

        check = AgentInspectorCheck()
        result = check.run(_make_context(repo_path=tmp_path))

        assert result.status == HealthStatus.HEALTHY
        assert "Could not parse" in result.reasoning

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_empty_response_graceful_healthy(self, mock_api, tmp_path):
        """Empty API response defaults to HEALTHY."""
        mock_api.return_value = ""

        check = AgentInspectorCheck()
        result = check.run(_make_context(repo_path=tmp_path))

        assert result.status == HealthStatus.HEALTHY

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_result_includes_raw_response(self, mock_api, tmp_path):
        """Result details include raw_response for debugging."""
        raw = '{"status": "HEALTHY", "reasoning": "OK"}'
        mock_api.return_value = raw

        check = AgentInspectorCheck()
        result = check.run(_make_context(repo_path=tmp_path))

        assert "raw_response" in result.details
        assert result.details["raw_response"] == raw

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_result_serializes_to_dict(self, mock_api, tmp_path):
        """Result to_dict() contains all required fields for SSE."""
        mock_api.return_value = '{"status": "DEGRADED", "reasoning": "Concern."}'

        check = AgentInspectorCheck()
        result = check.run(_make_context(repo_path=tmp_path))

        d = result.to_dict()
        assert d["status"] == "degraded"
        assert d["check_name"] == "agent_inspector"
        assert d["tier"] == "tier2"
        assert d["reasoning"] == "Concern."
        assert d["action"] == "alert"
        assert "timestamp" in d

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_generic_exception_graceful_degradation(self, mock_api, tmp_path):
        """Any unexpected exception results in HEALTHY with warning."""
        mock_api.side_effect = RuntimeError("Unexpected error")

        check = AgentInspectorCheck()
        result = check.run(_make_context(repo_path=tmp_path))

        assert result.status == HealthStatus.HEALTHY
        assert result.action == HealthAction.CONTINUE
        assert "unavailable" in result.reasoning
        assert result.details.get("graceful_degradation") is True


# ===========================================================================
# 6. Escalation Logic Tests (Runner + Tier 2)
# ===========================================================================


class _AlwaysHealthyTier1:
    name = "always_healthy"
    tier = HealthTier.PROGRAMMATIC
    triggers = frozenset({
        HealthTrigger.WAVE_COMPLETE,
        HealthTrigger.PHASE_COMPLETE,
        HealthTrigger.ON_DEMAND,
    })

    def run(self, context):
        return HealthResult(
            status=HealthStatus.HEALTHY,
            check_name=self.name,
            tier=self.tier,
            reasoning="All good.",
        )


class _DegradedTier1:
    name = "degraded_tier1"
    tier = HealthTier.PROGRAMMATIC
    triggers = frozenset({
        HealthTrigger.WAVE_COMPLETE,
        HealthTrigger.PHASE_COMPLETE,
    })

    def run(self, context):
        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning="Something is off.",
            action=HealthAction.ALERT,
        )


class _MockTier2:
    """Mock Tier 2 check that records whether it was called."""

    name = "mock_tier2"
    tier = HealthTier.AGENT
    triggers = frozenset({
        HealthTrigger.WAVE_COMPLETE,
        HealthTrigger.PHASE_COMPLETE,
        HealthTrigger.ON_DEMAND,
    })
    called = False

    def run(self, context):
        self.called = True
        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning="Tier 2 found issues.",
            action=HealthAction.ALERT,
        )


class TestEscalationLogic:
    """Tests for Tier 1 → Tier 2 escalation in HealthCheckRunner."""

    def test_wave_complete_healthy_no_tier2(self, tmp_path):
        """WAVE_COMPLETE with healthy Tier 1 does NOT run Tier 2."""
        runner = HealthCheckRunner()
        runner.register(_AlwaysHealthyTier1())
        tier2 = _MockTier2()
        tier2.called = False
        runner.register(tier2)

        ctx = _make_context(repo_path=tmp_path, trigger="wave_complete")
        results = runner.run(ctx, HealthTrigger.WAVE_COMPLETE)

        assert not tier2.called
        assert len(results) == 1  # Only Tier 1

    def test_wave_complete_degraded_runs_tier2(self, tmp_path):
        """WAVE_COMPLETE with degraded Tier 1 DOES run Tier 2."""
        runner = HealthCheckRunner()
        runner.register(_DegradedTier1())
        tier2 = _MockTier2()
        tier2.called = False
        runner.register(tier2)

        ctx = _make_context(repo_path=tmp_path, trigger="wave_complete")
        results = runner.run(ctx, HealthTrigger.WAVE_COMPLETE)

        assert tier2.called
        assert len(results) == 2  # Tier 1 + Tier 2

    def test_phase_complete_always_runs_tier2(self, tmp_path):
        """PHASE_COMPLETE always runs Tier 2, even if Tier 1 is healthy."""
        runner = HealthCheckRunner()
        runner.register(_AlwaysHealthyTier1())
        tier2 = _MockTier2()
        tier2.called = False
        runner.register(tier2)

        ctx = _make_context(repo_path=tmp_path, trigger="phase_complete")
        results = runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        assert tier2.called
        assert len(results) == 2

    def test_on_demand_always_runs_tier2(self, tmp_path):
        """ON_DEMAND always runs Tier 2."""
        runner = HealthCheckRunner()
        runner.register(_AlwaysHealthyTier1())
        tier2 = _MockTier2()
        tier2.called = False
        runner.register(tier2)

        ctx = _make_context(repo_path=tmp_path, trigger="on_demand")
        runner.run(ctx, HealthTrigger.ON_DEMAND)

        assert tier2.called

    def test_startup_never_runs_tier2(self, tmp_path):
        """STARTUP never runs Tier 2."""

        class StartupTier1:
            name = "startup_check"
            tier = HealthTier.PROGRAMMATIC
            triggers = frozenset({HealthTrigger.STARTUP})

            def run(self, context):
                return HealthResult(
                    status=HealthStatus.DEGRADED,
                    check_name=self.name,
                    tier=self.tier,
                    reasoning="Degraded at startup.",
                )

        class StartupTier2:
            name = "startup_tier2"
            tier = HealthTier.AGENT
            triggers = frozenset({HealthTrigger.STARTUP})
            called = False

            def run(self, context):
                self.called = True
                return HealthResult(
                    status=HealthStatus.HEALTHY,
                    check_name=self.name,
                    tier=self.tier,
                    reasoning="OK",
                )

        runner = HealthCheckRunner()
        tier1 = StartupTier1()
        tier2 = StartupTier2()
        runner.register(tier1)
        runner.register(tier2)

        ctx = _make_context(repo_path=tmp_path, trigger="startup")
        runner.run(ctx, HealthTrigger.STARTUP)

        assert not tier2.called

    def test_runtime_tick_never_runs_tier2(self, tmp_path):
        """RUNTIME_TICK never runs Tier 2."""

        class TickTier1:
            name = "tick_check"
            tier = HealthTier.PROGRAMMATIC
            triggers = frozenset({HealthTrigger.RUNTIME_TICK})

            def run(self, context):
                return HealthResult(
                    status=HealthStatus.DEGRADED,
                    check_name=self.name,
                    tier=self.tier,
                    reasoning="Degraded.",
                )

        class TickTier2:
            name = "tick_tier2"
            tier = HealthTier.AGENT
            triggers = frozenset({HealthTrigger.RUNTIME_TICK})
            called = False

            def run(self, context):
                self.called = True
                return HealthResult(
                    status=HealthStatus.HEALTHY,
                    check_name=self.name,
                    tier=self.tier,
                    reasoning="OK",
                )

        runner = HealthCheckRunner()
        runner.register(TickTier1())
        tier2 = TickTier2()
        runner.register(tier2)

        ctx = _make_context(repo_path=tmp_path, trigger="runtime_tick")
        runner.run(ctx, HealthTrigger.RUNTIME_TICK)

        assert not tier2.called


# ===========================================================================
# 7. Event Emission Tests
# ===========================================================================


class TestEventEmission:
    """Tests that Tier 2 results are emitted to EventBus for SSE."""

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_tier2_events_emitted(self, mock_api, tmp_path):
        """Tier 2 check results are emitted via EventBus."""
        mock_api.return_value = '{"status": "DEGRADED", "reasoning": "Concern."}'

        runner = HealthCheckRunner()
        runner.register(_AlwaysHealthyTier1())
        runner.register(AgentInspectorCheck())

        emitted_calls: list[tuple[tuple, dict]] = []

        # Mock the event bus
        mock_bus = MagicMock()
        mock_bus.emit = MagicMock(
            side_effect=lambda *args, **kwargs: emitted_calls.append((args, kwargs))
        )

        with patch.object(runner, "_get_event_bus", return_value=mock_bus):
            ctx = _make_context(repo_path=tmp_path, trigger="phase_complete")
            runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        # Should have: STARTED + per-check (tier1) + per-check (tier2) + COMPLETED
        assert len(emitted_calls) >= 4

        # Find the Tier 2 per-check event by looking at kwargs["data"]
        tier2_events = [
            (args, kwargs) for args, kwargs in emitted_calls
            if isinstance(kwargs.get("data"), dict) and kwargs["data"].get("tier") == "tier2"
        ]
        assert len(tier2_events) == 1

        # Verify payload completeness
        payload = tier2_events[0][1]["data"]
        assert payload["status"] == "degraded"
        assert payload["check_name"] == "agent_inspector"
        assert payload["tier"] == "tier2"
        assert "reasoning" in payload
        assert payload["action"] == "alert"

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_aggregate_event_includes_tier2(self, mock_api, tmp_path):
        """Aggregate COMPLETED event includes Tier 2 results."""
        mock_api.return_value = '{"status": "HEALTHY", "reasoning": "OK."}'

        runner = HealthCheckRunner()
        runner.register(_AlwaysHealthyTier1())
        runner.register(AgentInspectorCheck())

        emitted_calls: list[tuple[tuple, dict]] = []
        mock_bus = MagicMock()
        mock_bus.emit = MagicMock(
            side_effect=lambda *args, **kwargs: emitted_calls.append((args, kwargs))
        )

        with patch.object(runner, "_get_event_bus", return_value=mock_bus):
            ctx = _make_context(repo_path=tmp_path, trigger="phase_complete")
            runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        # Find the aggregate COMPLETED event (emit with "results" key in data)
        aggregate_events = [
            (args, kwargs) for args, kwargs in emitted_calls
            if isinstance(kwargs.get("data"), dict) and "results" in kwargs["data"]
        ]
        assert len(aggregate_events) == 1

        agg = aggregate_events[0][1]["data"]
        assert agg["check_count"] == 2
        tier_values = {r["tier"] for r in agg["results"]}
        assert "tier2" in tier_values


# ===========================================================================
# 8. Edge Cases
# ===========================================================================


class TestEdgeCases:
    """Edge case tests for Tier 2 health checks."""

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_no_api_key_graceful_degradation(self, mock_api, tmp_path):
        """Missing API key still triggers graceful degradation."""
        mock_api.side_effect = Exception("No API key configured")

        check = AgentInspectorCheck()
        result = check.run(_make_context(repo_path=tmp_path))

        assert result.status == HealthStatus.HEALTHY
        assert result.details.get("graceful_degradation") is True

    def test_context_with_no_repo(self, tmp_path):
        """Context works when pipeline has no repo field."""
        pipeline = Pipeline(
            id="test-no-repo",
            issue_number=42,
            repo=None,
            branch="egg/test",
            mode="issue",
            status=PipelineStatus.RUNNING,
            current_phase=PipelinePhase.IMPLEMENT,
        )
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        # Should not raise
        assert ctx.agent_outputs == {}
        assert ctx.contract == {}

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_concurrent_safety_multiple_runs(self, mock_api, tmp_path):
        """Multiple runs of the check don't share state unsafely."""
        mock_api.side_effect = [
            '{"status": "HEALTHY", "reasoning": "Run 1 OK."}',
            '{"status": "DEGRADED", "reasoning": "Run 2 concern."}',
        ]

        check = AgentInspectorCheck()
        r1 = check.run(_make_context(repo_path=tmp_path))
        r2 = check.run(_make_context(repo_path=tmp_path))

        assert r1.status == HealthStatus.HEALTHY
        assert r2.status == HealthStatus.DEGRADED
        assert r1 is not r2

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_raw_response_truncated_in_details(self, mock_api, tmp_path):
        """Raw response in details is capped at 500 chars."""
        long_response = '{"status": "HEALTHY", "reasoning": "' + "x" * 1000 + '"}'
        mock_api.return_value = long_response

        check = AgentInspectorCheck()
        result = check.run(_make_context(repo_path=tmp_path))

        assert len(result.details["raw_response"]) <= 500
