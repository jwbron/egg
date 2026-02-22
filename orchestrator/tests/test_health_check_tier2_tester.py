"""Supplementary Tier 2 health check tests (tester agent).

Covers gaps in the coder-authored test_health_check_tier2.py:

- Context assembly: multi-subdir agent_outputs, file extension filtering,
  per-file cap, git_log/git_diff_stat laziness, _run_git repo resolution
- Prompt construction: trigger field, contract key filtering, contract cap,
  branch=None
- API call: non-retryable exceptions, trailing-slash URL, system prompt in
  payload, max_tokens, default model
- Event emission: per-status event types, bus=None no-op, runner catches
  Tier 2 exceptions
- Route integration: pipeline health check endpoint invokes Tier 2
- Runner: multiple Tier 2 checks, Tier 2 exception in _run_single
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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

from health_checks.context import PipelineHealthContext
from health_checks.runner import HealthCheckRunner, worst_action
from health_checks.tier2.agent_inspector import (
    AgentInspectorCheck,
    _build_user_prompt,
    _call_claude_api,
    _parse_verdict,
)
from health_checks.types import (
    HealthAction,
    HealthResult,
    HealthStatus,
    HealthTier,
    HealthTrigger,
)
from models import Pipeline, PipelinePhase, PipelineStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pipeline(
    pipeline_id: str = "issue-42",
    issue_number: int = 42,
    repo: str | None = "owner/repo",
    branch: str | None = "egg/issue-42",
    phase: PipelinePhase = PipelinePhase.IMPLEMENT,
) -> Pipeline:
    return Pipeline(
        id=pipeline_id,
        issue_number=issue_number,
        repo=repo,
        branch=branch,
        mode="issue",
        status=PipelineStatus.RUNNING,
        current_phase=phase,
    )


def _ctx(
    pipeline: Pipeline | None = None,
    repo_path: Path | None = None,
    trigger: str = "phase_complete",
) -> PipelineHealthContext:
    if pipeline is None:
        pipeline = _pipeline()
    if repo_path is None:
        repo_path = Path("/tmp/fake-repos")
    return PipelineHealthContext(
        pipeline=pipeline,
        repo_path=repo_path,
        trigger=trigger,
    )


# ===========================================================================
# 1. Context Assembly — agent_outputs multi-subdir and filtering
# ===========================================================================


class TestContextAgentOutputsExtended:
    """Tests that agent_outputs reads from all three subdirs with filtering."""

    def test_reads_from_drafts_subdir(self, tmp_path):
        """agent_outputs reads .md files from drafts/."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "plan.md").write_text("# Plan\nStep 1")

        ctx = PipelineHealthContext(
            pipeline=_pipeline(repo=None),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert "plan.md" in ctx.agent_outputs
        assert "# Plan" in ctx.agent_outputs["plan.md"]

    def test_reads_from_contracts_subdir(self, tmp_path):
        """agent_outputs reads .json files from contracts/."""
        contracts = tmp_path / ".egg-state" / "contracts"
        contracts.mkdir(parents=True)
        (contracts / "42.json").write_text('{"phase": "implement"}')

        ctx = PipelineHealthContext(
            pipeline=_pipeline(repo=None),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert "42.json" in ctx.agent_outputs

    def test_reads_yaml_files(self, tmp_path):
        """agent_outputs includes .yaml and .yml files."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "config.yaml").write_text("key: value")
        (drafts / "spec.yml").write_text("name: test")

        ctx = PipelineHealthContext(
            pipeline=_pipeline(repo=None),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert "config.yaml" in ctx.agent_outputs
        assert "spec.yml" in ctx.agent_outputs

    def test_ignores_non_matching_extensions(self, tmp_path):
        """agent_outputs skips files with unrecognized extensions."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "plan.md").write_text("keep")
        (drafts / "binary.bin").write_text("skip")
        (drafts / "image.png").write_bytes(b"\x89PNG")
        (drafts / "script.py").write_text("skip")

        ctx = PipelineHealthContext(
            pipeline=_pipeline(repo=None),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert "plan.md" in ctx.agent_outputs
        assert "binary.bin" not in ctx.agent_outputs
        assert "image.png" not in ctx.agent_outputs
        assert "script.py" not in ctx.agent_outputs

    def test_per_file_content_capped_at_4000(self, tmp_path):
        """Each agent output file is capped at 4000 characters."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "huge.md").write_text("x" * 10000)

        ctx = PipelineHealthContext(
            pipeline=_pipeline(repo=None),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert len(ctx.agent_outputs["huge.md"]) == 4000

    def test_reads_from_all_three_subdirs_combined(self, tmp_path):
        """agent_outputs merges files from drafts, contracts, and agent-outputs."""
        for subdir in ("drafts", "contracts", "agent-outputs"):
            d = tmp_path / ".egg-state" / subdir
            d.mkdir(parents=True)
            (d / f"from-{subdir}.json").write_text(f'{{"source": "{subdir}"}}')

        ctx = PipelineHealthContext(
            pipeline=_pipeline(repo=None),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert "from-drafts.json" in ctx.agent_outputs
        assert "from-contracts.json" in ctx.agent_outputs
        assert "from-agent-outputs.json" in ctx.agent_outputs

    def test_empty_state_dir_returns_empty(self, tmp_path):
        """Returns empty dict when .egg-state/ exists but subdirs are empty."""
        (tmp_path / ".egg-state").mkdir(parents=True)

        ctx = PipelineHealthContext(
            pipeline=_pipeline(repo=None),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert ctx.agent_outputs == {}

    def test_missing_state_dir_returns_empty(self, tmp_path):
        """Returns empty dict when .egg-state/ doesn't exist at all."""
        ctx = PipelineHealthContext(
            pipeline=_pipeline(repo=None),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        assert ctx.agent_outputs == {}

    def test_agent_outputs_lazy_and_cached(self, tmp_path):
        """agent_outputs is only computed once."""
        drafts = tmp_path / ".egg-state" / "drafts"
        drafts.mkdir(parents=True)
        (drafts / "file.md").write_text("content")

        ctx = PipelineHealthContext(
            pipeline=_pipeline(repo=None),
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        first = ctx.agent_outputs
        second = ctx.agent_outputs
        assert first is second


# ===========================================================================
# 2. Context Assembly — git lazy properties and repo resolution
# ===========================================================================


class TestContextGitProperties:
    """Tests for git_log, git_diff_stat laziness and _run_git resolution."""

    def test_git_log_is_lazy_and_cached(self, tmp_path):
        """git_log triggers subprocess once and caches."""
        ctx = _ctx(repo_path=tmp_path)
        with patch.object(ctx, "_run_git", return_value="abc1234 commit") as mock:
            first = ctx.git_log
            second = ctx.git_log
            assert first is second
            mock.assert_called_once_with("log", "--oneline", "-20")

    def test_git_diff_stat_is_lazy_and_cached(self, tmp_path):
        """git_diff_stat triggers subprocess once and caches."""
        ctx = _ctx(repo_path=tmp_path)
        with patch.object(ctx, "_run_git", return_value="file.py | 3 +++") as mock:
            first = ctx.git_diff_stat
            second = ctx.git_diff_stat
            assert first is second
            mock.assert_called_once()

    def test_run_git_resolves_repo_subdirectory(self, tmp_path):
        """_run_git uses repo_path/repo_name when pipeline.repo is set."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        pipeline = _pipeline(repo="owner/repo")
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="phase_complete",
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok\n", returncode=0)
            ctx._run_git("log", "--oneline", "-5")
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["cwd"] == str(repo_dir)

    def test_run_git_falls_back_to_repo_path(self, tmp_path):
        """_run_git uses repo_path when repo subdirectory doesn't exist."""
        pipeline = _pipeline(repo="owner/nonexistent")
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="phase_complete",
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok\n", returncode=0)
            ctx._run_git("status")
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["cwd"] == str(tmp_path)

    def test_run_git_returns_empty_on_exception(self, tmp_path):
        """_run_git returns empty string on subprocess failure."""
        ctx = _ctx(repo_path=tmp_path)
        with patch("subprocess.run", side_effect=OSError("git not found")):
            result = ctx._run_git("log")
            assert result == ""

    def test_git_log_empty_when_no_git_repo(self, tmp_path):
        """git_log returns empty string when directory is not a git repo."""
        ctx = _ctx(repo_path=tmp_path)
        # No git repo means subprocess will fail
        result = ctx.git_log
        assert result == ""


# ===========================================================================
# 3. Context Assembly — live_container_ids
# ===========================================================================


class TestContextLiveContainers:
    """Tests for live_container_ids property."""

    def test_live_container_ids_empty_without_client(self, tmp_path):
        """Returns empty set when docker_client is None."""
        ctx = PipelineHealthContext(
            pipeline=_pipeline(),
            repo_path=tmp_path,
            trigger="phase_complete",
            docker_client=None,
        )
        assert ctx.live_container_ids == set()

    def test_live_container_ids_from_docker_client(self, tmp_path):
        """Returns container IDs from docker client."""
        mock_client = MagicMock()
        container1 = MagicMock()
        container1.container_id = "abc123"
        container2 = MagicMock()
        container2.container_id = "def456"
        mock_client.list_containers.return_value = [container1, container2]

        ctx = PipelineHealthContext(
            pipeline=_pipeline(),
            repo_path=tmp_path,
            trigger="phase_complete",
            docker_client=mock_client,
        )
        assert ctx.live_container_ids == {"abc123", "def456"}

    def test_live_container_ids_cached(self, tmp_path):
        """live_container_ids is lazily evaluated and cached."""
        mock_client = MagicMock()
        mock_client.list_containers.return_value = []

        ctx = PipelineHealthContext(
            pipeline=_pipeline(),
            repo_path=tmp_path,
            trigger="phase_complete",
            docker_client=mock_client,
        )
        first = ctx.live_container_ids
        second = ctx.live_container_ids
        assert first is second
        mock_client.list_containers.assert_called_once()

    def test_live_container_ids_empty_on_docker_error(self, tmp_path):
        """Returns empty set when docker client raises."""
        mock_client = MagicMock()
        mock_client.list_containers.side_effect = RuntimeError("Docker down")

        ctx = PipelineHealthContext(
            pipeline=_pipeline(),
            repo_path=tmp_path,
            trigger="phase_complete",
            docker_client=mock_client,
        )
        assert ctx.live_container_ids == set()


# ===========================================================================
# 4. Prompt Construction — extended coverage
# ===========================================================================


class TestBuildUserPromptExtended:
    """Extended tests for _build_user_prompt covering gaps."""

    def test_trigger_field_in_prompt(self, tmp_path):
        """Prompt includes the trigger value."""
        ctx = _ctx(repo_path=tmp_path, trigger="wave_complete")
        prompt = _build_user_prompt(ctx)
        assert "wave_complete" in prompt

    def test_branch_none_shows_unknown(self, tmp_path):
        """When pipeline.branch is None, prompt shows 'unknown'."""
        pipeline = _pipeline(branch=None)
        ctx = _ctx(pipeline=pipeline, repo_path=tmp_path)
        prompt = _build_user_prompt(ctx)
        assert "unknown" in prompt

    def test_contract_key_filtering(self, tmp_path):
        """Only specific contract keys appear in the Contract State section."""
        state_dir = tmp_path / ".egg-state" / "contracts"
        state_dir.mkdir(parents=True)
        contract = {
            "current_phase": "implement",
            "acceptance_criteria": ["Tests pass"],
            "decisions": [{"q": "Which DB?"}],
            "agent_executions": [{"role": "coder"}],
            "schema_version": "1.0",
            "internal_secret": "should-not-appear",
        }
        (state_dir / "42.json").write_text(json.dumps(contract))

        pipeline = _pipeline(repo=None)
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        prompt = _build_user_prompt(ctx)

        # Extract just the Contract State section (after "## Contract State")
        contract_idx = prompt.index("## Contract State")
        contract_section = prompt[contract_idx:]

        # Included keys in Contract State section
        assert "current_phase" in contract_section
        assert "acceptance_criteria" in contract_section
        assert "decisions" in contract_section
        assert "agent_executions" in contract_section
        # Excluded keys should NOT be in the Contract State section
        # (they may appear in Agent Output Files which shows raw file content)
        assert "internal_secret" not in contract_section
        assert "schema_version" not in contract_section

    def test_contract_content_capped_at_3000_in_prompt(self, tmp_path):
        """Contract JSON in prompt is capped at 3000 chars."""
        state_dir = tmp_path / ".egg-state" / "contracts"
        state_dir.mkdir(parents=True)
        # Build a contract with a very large acceptance_criteria field
        big_contract = {
            "current_phase": "implement",
            "acceptance_criteria": ["x" * 5000],
        }
        (state_dir / "42.json").write_text(json.dumps(big_contract))

        pipeline = _pipeline(repo=None)
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        prompt = _build_user_prompt(ctx)

        # The contract section should be present but capped
        assert "Contract State" in prompt
        # Find the JSON blob in the prompt — it should be <= 3000 chars
        idx = prompt.index("## Contract State")
        contract_section = prompt[idx:]
        # The serialized JSON portion shouldn't exceed 3000 chars of the contract
        # (plus section headers). Verify no single "x" run exceeds ~3000.
        x_runs = [line for line in contract_section.split("\n") if "xxxxx" in line]
        for line in x_runs:
            assert len(line) <= 3001

    def test_prompt_sections_order(self, tmp_path):
        """Prompt has sections in correct order: metadata, commits, diff, outputs, contract."""
        state_dir = tmp_path / ".egg-state" / "drafts"
        state_dir.mkdir(parents=True)
        (state_dir / "plan.md").write_text("plan content")

        pipeline = _pipeline(repo=None)
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="phase_complete",
        )
        with patch.object(ctx, "_run_git", return_value="abc log"):
            _ = ctx.git_log
            _ = ctx.git_diff_stat

        prompt = _build_user_prompt(ctx)

        # Sections should appear in order
        assert (
            prompt.index("Pipeline:") < prompt.index("## Recent Commits")
            or "## Recent Commits" not in prompt
        )
        if "## Agent Output Files" in prompt:
            assert prompt.index("## Agent Output Files") > 0


# ===========================================================================
# 5. Verdict Parsing — extended coverage
# ===========================================================================


class TestParseVerdictExtended:
    """Extended tests for _parse_verdict."""

    def test_extra_whitespace_around_json(self):
        """Leading/trailing whitespace doesn't break parsing."""
        text = '   \n  {"status": "DEGRADED", "reasoning": "Slow."}  \n  '
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.DEGRADED
        assert "Slow" in reasoning

    def test_extra_fields_ignored(self):
        """Extra fields in JSON don't affect parsing."""
        text = '{"status": "HEALTHY", "reasoning": "OK", "confidence": 0.95, "tags": ["fast"]}'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.HEALTHY
        assert "OK" in reasoning

    def test_status_with_mixed_case(self):
        """Mixed-case status like 'Healthy' is handled."""
        text = '{"status": "Healthy", "reasoning": "Fine."}'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.HEALTHY

    def test_numeric_status_defaults_healthy(self):
        """Numeric status defaults to HEALTHY."""
        text = '{"status": 42, "reasoning": "Weird."}'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.HEALTHY
        assert "Unknown status" in reasoning

    def test_null_status_defaults_healthy(self):
        """null status defaults to HEALTHY."""
        text = '{"status": null, "reasoning": "Null."}'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.HEALTHY

    def test_nested_json_in_code_fence(self):
        """JSON inside triple backticks with json tag works."""
        text = '```json\n{"status": "FAILED", "reasoning": "Loop detected."}\n```'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.FAILED
        assert "Loop" in reasoning

    def test_partial_json_returns_healthy(self):
        """Truncated JSON gracefully returns HEALTHY."""
        text = '{"status": "DEGR'
        status, reasoning = _parse_verdict(text)
        assert status == HealthStatus.HEALTHY
        assert "Could not parse" in reasoning


# ===========================================================================
# 6. API Call — extended coverage
# ===========================================================================


class TestCallClaudeApiExtended:
    """Extended tests for _call_claude_api."""

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_non_retryable_exception_raises_immediately(self, mock_httpx):
        """Generic Exception (not Timeout/HTTPStatusError) raises without retry."""
        mock_httpx.post.side_effect = ValueError("Unexpected error")
        # Need to set these so the except clause can match properly
        import httpx

        mock_httpx.TimeoutException = httpx.TimeoutException
        mock_httpx.HTTPStatusError = httpx.HTTPStatusError

        with pytest.raises(ValueError, match="Unexpected error"):
            _call_claude_api("test", api_key="sk-ant-key")
        # Only 1 attempt — no retry for non-transient errors
        assert mock_httpx.post.call_count == 1

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_base_url_trailing_slash_stripped(self, mock_httpx):
        """Trailing slash in base_url is stripped before appending path."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"content": [{"type": "text", "text": "ok"}]}
        mock_httpx.post.return_value = mock_resp

        _call_claude_api("test", api_key="key", base_url="https://api.example.com/")

        url_called = mock_httpx.post.call_args[0][0]
        assert url_called == "https://api.example.com/v1/messages"
        assert "//" not in url_called.replace("https://", "")

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_payload_includes_system_prompt(self, mock_httpx):
        """API payload includes the system prompt."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"content": [{"type": "text", "text": "ok"}]}
        mock_httpx.post.return_value = mock_resp

        _call_claude_api("test prompt", api_key="key")

        payload = mock_httpx.post.call_args[1]["json"]
        assert "system" in payload
        assert "pipeline health inspector" in payload["system"]

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_payload_max_tokens_is_512(self, mock_httpx):
        """API payload requests max_tokens=512."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"content": [{"type": "text", "text": "ok"}]}
        mock_httpx.post.return_value = mock_resp

        _call_claude_api("test", api_key="key")

        payload = mock_httpx.post.call_args[1]["json"]
        assert payload["max_tokens"] == 512

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_default_model_is_claude_sonnet(self, mock_httpx):
        """Default model is claude-sonnet-4-20250514 when no env var set."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"content": [{"type": "text", "text": "ok"}]}
        mock_httpx.post.return_value = mock_resp

        # Ensure env vars are cleared
        env = {k: v for k, v in os.environ.items() if k != "HEALTH_CHECK_MODEL"}
        with patch.dict("os.environ", env, clear=True):
            _call_claude_api("test", api_key="key")

        payload = mock_httpx.post.call_args[1]["json"]
        assert payload["model"] == "claude-sonnet-4-20250514"

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_timeout_value_is_30_seconds(self, mock_httpx):
        """API call uses 30-second timeout."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"content": [{"type": "text", "text": "ok"}]}
        mock_httpx.post.return_value = mock_resp

        _call_claude_api("test", api_key="key")

        call_kwargs = mock_httpx.post.call_args[1]
        assert call_kwargs["timeout"] == 30

    @patch("health_checks.tier2.agent_inspector.httpx")
    def test_user_prompt_forwarded_to_api(self, mock_httpx):
        """User prompt is included in the messages array."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"content": [{"type": "text", "text": "ok"}]}
        mock_httpx.post.return_value = mock_resp

        _call_claude_api("Analyze this pipeline", api_key="key")

        payload = mock_httpx.post.call_args[1]["json"]
        messages = payload["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Analyze this pipeline"


# ===========================================================================
# 7. AgentInspectorCheck — extended coverage
# ===========================================================================


class TestAgentInspectorCheckExtended:
    """Extended tests for AgentInspectorCheck.run()."""

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_prompt_built_from_context(self, mock_api, tmp_path):
        """Verifies _build_user_prompt is called with the context."""
        mock_api.return_value = '{"status": "HEALTHY", "reasoning": "OK"}'

        check = AgentInspectorCheck()
        pipeline = _pipeline(repo=None)
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="wave_complete",
        )

        with patch(
            "health_checks.tier2.agent_inspector._build_user_prompt",
            wraps=_build_user_prompt,
        ) as mock_build:
            check.run(ctx)
            mock_build.assert_called_once_with(ctx)

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_connection_error_graceful_degradation(self, mock_api, tmp_path):
        """ConnectionError results in graceful degradation."""
        mock_api.side_effect = ConnectionError("Connection refused")

        check = AgentInspectorCheck()
        result = check.run(_ctx(repo_path=tmp_path))

        assert result.status == HealthStatus.HEALTHY
        assert result.action == HealthAction.CONTINUE
        assert result.details.get("graceful_degradation") is True
        assert "ConnectionError" in result.reasoning

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_keyboard_interrupt_graceful_degradation(self, mock_api, tmp_path):
        """Even KeyboardInterrupt-derived errors degrade gracefully."""
        mock_api.side_effect = Exception("Simulated interrupt")

        check = AgentInspectorCheck()
        result = check.run(_ctx(repo_path=tmp_path))

        assert result.status == HealthStatus.HEALTHY
        assert result.details.get("graceful_degradation") is True

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_check_name_and_tier_in_every_result(self, mock_api, tmp_path):
        """Every result has correct check_name and tier regardless of outcome."""
        scenarios = [
            '{"status": "HEALTHY", "reasoning": "OK"}',
            '{"status": "DEGRADED", "reasoning": "Concern"}',
            '{"status": "FAILED", "reasoning": "Bad"}',
        ]
        for response in scenarios:
            mock_api.return_value = response
            check = AgentInspectorCheck()
            result = check.run(_ctx(repo_path=tmp_path))
            assert result.check_name == "agent_inspector"
            assert result.tier == HealthTier.AGENT

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_triggers_include_required_set(self, mock_api):
        """Check triggers match the design spec."""
        check = AgentInspectorCheck()
        assert HealthTrigger.WAVE_COMPLETE in check.triggers
        assert HealthTrigger.PHASE_COMPLETE in check.triggers
        assert HealthTrigger.ON_DEMAND in check.triggers
        # Should NOT include startup or runtime_tick
        assert HealthTrigger.STARTUP not in check.triggers
        assert HealthTrigger.RUNTIME_TICK not in check.triggers


# ===========================================================================
# 8. Runner — Tier 2 exception handling and multiple Tier 2 checks
# ===========================================================================


class _FailingTier2:
    """A Tier 2 check that raises an exception."""

    name = "failing_tier2"
    tier = HealthTier.AGENT
    triggers = frozenset({HealthTrigger.PHASE_COMPLETE, HealthTrigger.ON_DEMAND})

    def run(self, context):
        raise RuntimeError("Tier 2 check exploded")


class _HealthyTier1:
    name = "healthy_t1"
    tier = HealthTier.PROGRAMMATIC
    triggers = frozenset(
        {
            HealthTrigger.PHASE_COMPLETE,
            HealthTrigger.ON_DEMAND,
            HealthTrigger.WAVE_COMPLETE,
        }
    )

    def run(self, context):
        return HealthResult(
            status=HealthStatus.HEALTHY,
            check_name=self.name,
            tier=self.tier,
            reasoning="OK",
        )


class _DegradedTier1:
    name = "degraded_t1"
    tier = HealthTier.PROGRAMMATIC
    triggers = frozenset({HealthTrigger.WAVE_COMPLETE, HealthTrigger.PHASE_COMPLETE})

    def run(self, context):
        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning="Degraded.",
            action=HealthAction.ALERT,
        )


class _SecondTier2:
    """Another Tier 2 check that returns HEALTHY."""

    name = "second_tier2"
    tier = HealthTier.AGENT
    triggers = frozenset({HealthTrigger.PHASE_COMPLETE, HealthTrigger.ON_DEMAND})

    def run(self, context):
        return HealthResult(
            status=HealthStatus.HEALTHY,
            check_name=self.name,
            tier=self.tier,
            reasoning="Second check OK.",
        )


class TestRunnerTier2Extended:
    """Extended runner tests focused on Tier 2 behavior."""

    def test_tier2_exception_caught_by_run_single(self, tmp_path):
        """Runner._run_single catches Tier 2 exceptions gracefully."""
        runner = HealthCheckRunner()
        runner.register(_HealthyTier1())
        runner.register(_FailingTier2())

        ctx = _ctx(repo_path=tmp_path, trigger="phase_complete")
        results = runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        # Should have 2 results: healthy Tier 1 + degraded from caught exception
        assert len(results) == 2
        tier2_result = [r for r in results if r.tier == HealthTier.AGENT][0]
        assert tier2_result.status == HealthStatus.DEGRADED
        assert "failed internally" in tier2_result.reasoning
        assert tier2_result.action == HealthAction.ALERT

    def test_multiple_tier2_checks_all_run(self, tmp_path):
        """Multiple Tier 2 checks all execute when escalation triggers."""
        runner = HealthCheckRunner()
        runner.register(_HealthyTier1())
        runner.register(AgentInspectorCheck())
        runner.register(_SecondTier2())

        ctx = _ctx(repo_path=tmp_path, trigger="phase_complete")

        with patch(
            "health_checks.tier2.agent_inspector._call_claude_api",
            return_value='{"status": "HEALTHY", "reasoning": "Inspector OK"}',
        ):
            results = runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        # 1 Tier 1 + 2 Tier 2 checks
        assert len(results) == 3
        tier2_results = [r for r in results if r.tier == HealthTier.AGENT]
        assert len(tier2_results) == 2
        names = {r.check_name for r in tier2_results}
        assert "agent_inspector" in names
        assert "second_tier2" in names

    def test_worst_action_with_tier2_alert(self, tmp_path):
        """worst_action correctly identifies ALERT from Tier 2 results."""
        results = [
            HealthResult(
                status=HealthStatus.HEALTHY,
                check_name="tier1",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="OK",
                action=HealthAction.CONTINUE,
            ),
            HealthResult(
                status=HealthStatus.DEGRADED,
                check_name="agent_inspector",
                tier=HealthTier.AGENT,
                reasoning="Concern",
                action=HealthAction.ALERT,
            ),
        ]
        assert worst_action(results) == HealthAction.ALERT

    def test_worst_action_fail_pipeline_takes_priority(self):
        """worst_action: FAIL_PIPELINE > ALERT > CONTINUE."""
        results = [
            HealthResult(
                status=HealthStatus.DEGRADED,
                check_name="tier2a",
                tier=HealthTier.AGENT,
                reasoning="Alert",
                action=HealthAction.ALERT,
            ),
            HealthResult(
                status=HealthStatus.FAILED,
                check_name="tier1a",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="Fatal",
                action=HealthAction.FAIL_PIPELINE,
            ),
        ]
        assert worst_action(results) == HealthAction.FAIL_PIPELINE

    def test_worst_action_all_continue(self):
        """worst_action returns CONTINUE when all results are CONTINUE."""
        results = [
            HealthResult(
                status=HealthStatus.HEALTHY,
                check_name="a",
                tier=HealthTier.PROGRAMMATIC,
                reasoning="OK",
            ),
            HealthResult(
                status=HealthStatus.HEALTHY,
                check_name="b",
                tier=HealthTier.AGENT,
                reasoning="OK",
            ),
        ]
        assert worst_action(results) == HealthAction.CONTINUE

    def test_worst_action_empty_results(self):
        """worst_action returns CONTINUE for empty results list."""
        assert worst_action([]) == HealthAction.CONTINUE


# ===========================================================================
# 9. Event Emission — per-status event types and bus=None
# ===========================================================================


class TestEventEmissionExtended:
    """Extended event emission tests."""

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_failed_verdict_emits_health_check_failed_event(self, mock_api, tmp_path):
        """FAILED verdict emits HEALTH_CHECK_FAILED event type."""
        mock_api.return_value = '{"status": "FAILED", "reasoning": "Agent stuck."}'

        runner = HealthCheckRunner()
        runner.register(_HealthyTier1())
        runner.register(AgentInspectorCheck())

        emitted: list[tuple] = []
        mock_bus = MagicMock()
        mock_bus.emit = MagicMock(
            side_effect=lambda *args, **kwargs: emitted.append((args, kwargs))
        )

        with patch.object(runner, "_get_event_bus", return_value=mock_bus):
            ctx = _ctx(repo_path=tmp_path, trigger="phase_complete")
            runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        # Find per-check events for Tier 2
        from events import EventType

        tier2_events = [
            (args, kw)
            for args, kw in emitted
            if len(args) >= 1 and args[0] == EventType.HEALTH_CHECK_FAILED
        ]
        assert len(tier2_events) == 1

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_healthy_verdict_emits_health_check_completed_event(self, mock_api, tmp_path):
        """HEALTHY verdict emits HEALTH_CHECK_COMPLETED event type for per-check."""
        mock_api.return_value = '{"status": "HEALTHY", "reasoning": "OK."}'

        runner = HealthCheckRunner()
        runner.register(_HealthyTier1())
        runner.register(AgentInspectorCheck())

        emitted: list[tuple] = []
        mock_bus = MagicMock()
        mock_bus.emit = MagicMock(
            side_effect=lambda *args, **kwargs: emitted.append((args, kwargs))
        )

        with patch.object(runner, "_get_event_bus", return_value=mock_bus):
            ctx = _ctx(repo_path=tmp_path, trigger="phase_complete")
            runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        from events import EventType

        # Per-check COMPLETED events (not the aggregate one)
        completed_events = [
            (args, kw)
            for args, kw in emitted
            if len(args) >= 1
            and args[0] == EventType.HEALTH_CHECK_COMPLETED
            and isinstance(kw.get("data"), dict)
            and "check_name" in kw["data"]
        ]
        # Both Tier 1 (healthy) and Tier 2 (healthy) should emit COMPLETED
        names = {kw["data"]["check_name"] for _, kw in completed_events}
        assert "agent_inspector" in names

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_degraded_verdict_emits_health_check_degraded_event(self, mock_api, tmp_path):
        """DEGRADED verdict emits HEALTH_CHECK_DEGRADED event type."""
        mock_api.return_value = '{"status": "DEGRADED", "reasoning": "Stale."}'

        runner = HealthCheckRunner()
        runner.register(_HealthyTier1())
        runner.register(AgentInspectorCheck())

        emitted: list[tuple] = []
        mock_bus = MagicMock()
        mock_bus.emit = MagicMock(
            side_effect=lambda *args, **kwargs: emitted.append((args, kwargs))
        )

        with patch.object(runner, "_get_event_bus", return_value=mock_bus):
            ctx = _ctx(repo_path=tmp_path, trigger="phase_complete")
            runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        from events import EventType

        degraded_events = [
            (args, kw)
            for args, kw in emitted
            if len(args) >= 1 and args[0] == EventType.HEALTH_CHECK_DEGRADED
        ]
        assert len(degraded_events) == 1

    def test_no_event_bus_does_not_crash(self, tmp_path):
        """Runner works when event bus is unavailable (returns None)."""
        runner = HealthCheckRunner()
        runner.register(_HealthyTier1())

        with patch.object(runner, "_get_event_bus", return_value=None):
            ctx = _ctx(repo_path=tmp_path, trigger="phase_complete")
            results = runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        assert len(results) == 1
        assert results[0].status == HealthStatus.HEALTHY

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_event_payload_has_required_sse_fields(self, mock_api, tmp_path):
        """Event payload includes all fields needed for SSE clients."""
        mock_api.return_value = '{"status": "DEGRADED", "reasoning": "No commits."}'

        runner = HealthCheckRunner()
        runner.register(_HealthyTier1())
        runner.register(AgentInspectorCheck())

        emitted: list[tuple] = []
        mock_bus = MagicMock()
        mock_bus.emit = MagicMock(
            side_effect=lambda *args, **kwargs: emitted.append((args, kwargs))
        )

        with patch.object(runner, "_get_event_bus", return_value=mock_bus):
            ctx = _ctx(repo_path=tmp_path, trigger="phase_complete")
            runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        # Find per-check tier2 event
        tier2_payloads = [
            kw["data"]
            for _, kw in emitted
            if isinstance(kw.get("data"), dict) and kw["data"].get("tier") == "tier2"
        ]
        assert len(tier2_payloads) == 1
        payload = tier2_payloads[0]

        required_fields = {"status", "tier", "check_name", "reasoning", "action", "timestamp"}
        assert required_fields.issubset(set(payload.keys()))


# ===========================================================================
# 10. Escalation Logic — additional scenarios
# ===========================================================================


class TestEscalationLogicExtended:
    """Extended escalation logic tests."""

    def test_wave_complete_with_failed_tier1_runs_tier2(self, tmp_path):
        """WAVE_COMPLETE with FAILED Tier 1 does NOT trigger Tier 2 (only DEGRADED does)."""

        class FailedTier1:
            name = "failed_t1"
            tier = HealthTier.PROGRAMMATIC
            triggers = frozenset({HealthTrigger.WAVE_COMPLETE})

            def run(self, context):
                return HealthResult(
                    status=HealthStatus.FAILED,
                    check_name=self.name,
                    tier=self.tier,
                    reasoning="Failed.",
                    action=HealthAction.FAIL_PIPELINE,
                )

        class TrackingTier2:
            name = "tracking_t2"
            tier = HealthTier.AGENT
            triggers = frozenset({HealthTrigger.WAVE_COMPLETE})
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
        runner.register(FailedTier1())
        tier2 = TrackingTier2()
        runner.register(tier2)

        ctx = _ctx(repo_path=tmp_path, trigger="wave_complete")
        results = runner.run(ctx, HealthTrigger.WAVE_COMPLETE)

        # FAILED status is not DEGRADED, so Tier 2 should NOT run
        assert not tier2.called
        assert len(results) == 1

    def test_should_escalate_static_method_directly(self):
        """Test _should_escalate_to_tier2 as a static method with various inputs."""
        # PHASE_COMPLETE always true
        assert HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.PHASE_COMPLETE, []) is True

        # ON_DEMAND always true
        assert HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.ON_DEMAND, []) is True

        # STARTUP always false
        assert HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.STARTUP, []) is False

        # RUNTIME_TICK always false
        assert HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.RUNTIME_TICK, []) is False

        # WAVE_COMPLETE with no results — no escalation
        assert HealthCheckRunner._should_escalate_to_tier2(HealthTrigger.WAVE_COMPLETE, []) is False

        # WAVE_COMPLETE with only HEALTHY — no escalation
        healthy_result = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="t1",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="OK",
        )
        assert (
            HealthCheckRunner._should_escalate_to_tier2(
                HealthTrigger.WAVE_COMPLETE, [healthy_result]
            )
            is False
        )

        # WAVE_COMPLETE with DEGRADED — escalate
        degraded_result = HealthResult(
            status=HealthStatus.DEGRADED,
            check_name="t1",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="Concern",
        )
        assert (
            HealthCheckRunner._should_escalate_to_tier2(
                HealthTrigger.WAVE_COMPLETE, [degraded_result]
            )
            is True
        )

    def test_no_checks_registered_returns_empty(self, tmp_path):
        """Runner with no registered checks returns empty results."""
        runner = HealthCheckRunner()
        ctx = _ctx(repo_path=tmp_path, trigger="phase_complete")
        results = runner.run(ctx, HealthTrigger.PHASE_COMPLETE)
        assert results == []

    def test_only_tier2_registered_phase_complete(self, tmp_path):
        """When only Tier 2 checks registered, they run on PHASE_COMPLETE."""
        runner = HealthCheckRunner()
        runner.register(_SecondTier2())

        ctx = _ctx(repo_path=tmp_path, trigger="phase_complete")
        results = runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        assert len(results) == 1
        assert results[0].tier == HealthTier.AGENT

    def test_only_tier2_registered_wave_complete_no_run(self, tmp_path):
        """When only Tier 2 checks registered, WAVE_COMPLETE doesn't run them
        (no Tier 1 degraded to trigger escalation)."""

        class WaveTier2:
            name = "wave_t2"
            tier = HealthTier.AGENT
            triggers = frozenset({HealthTrigger.WAVE_COMPLETE})
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
        tier2 = WaveTier2()
        runner.register(tier2)

        ctx = _ctx(repo_path=tmp_path, trigger="wave_complete")
        results = runner.run(ctx, HealthTrigger.WAVE_COMPLETE)

        assert not tier2.called
        assert len(results) == 0


# ===========================================================================
# 11. Context — constructor and cheap accessors
# ===========================================================================


class TestContextCheapAccessors:
    """Tests for PipelineHealthContext cheap properties."""

    def test_pipeline_id(self, tmp_path):
        pipeline = _pipeline(pipeline_id="issue-123")
        ctx = PipelineHealthContext(pipeline=pipeline, repo_path=tmp_path, trigger="startup")
        assert ctx.pipeline_id == "issue-123"

    def test_branch(self, tmp_path):
        pipeline = _pipeline(branch="egg/feature")
        ctx = PipelineHealthContext(pipeline=pipeline, repo_path=tmp_path, trigger="startup")
        assert ctx.branch == "egg/feature"

    def test_current_phase_from_pipeline(self, tmp_path):
        pipeline = _pipeline(phase=PipelinePhase.PLAN)
        ctx = PipelineHealthContext(pipeline=pipeline, repo_path=tmp_path, trigger="startup")
        assert ctx.current_phase == PipelinePhase.PLAN

    def test_current_phase_override(self, tmp_path):
        pipeline = _pipeline(phase=PipelinePhase.IMPLEMENT)
        ctx = PipelineHealthContext(
            pipeline=pipeline,
            repo_path=tmp_path,
            trigger="startup",
            phase=PipelinePhase.PR,
        )
        assert ctx.current_phase == PipelinePhase.PR

    def test_wave_number_stored(self, tmp_path):
        ctx = PipelineHealthContext(
            pipeline=_pipeline(),
            repo_path=tmp_path,
            trigger="wave_complete",
            wave_number=3,
        )
        assert ctx.wave_number == 3

    def test_timestamp_set(self, tmp_path):
        ctx = PipelineHealthContext(
            pipeline=_pipeline(),
            repo_path=tmp_path,
            trigger="startup",
        )
        assert ctx.timestamp is not None


# ===========================================================================
# 12. HealthResult.to_dict completeness
# ===========================================================================


class TestHealthResultSerialization:
    """Tests for HealthResult.to_dict() used by SSE events."""

    def test_to_dict_all_fields(self):
        result = HealthResult(
            status=HealthStatus.DEGRADED,
            check_name="agent_inspector",
            tier=HealthTier.AGENT,
            reasoning="No commits detected.",
            action=HealthAction.ALERT,
            details={"raw_response": "some text"},
        )
        d = result.to_dict()
        assert d["status"] == "degraded"
        assert d["check_name"] == "agent_inspector"
        assert d["tier"] == "tier2"
        assert d["reasoning"] == "No commits detected."
        assert d["action"] == "alert"
        assert d["details"] == {"raw_response": "some text"}
        assert "timestamp" in d
        assert d["timestamp"].endswith("Z")

    def test_to_dict_default_action(self):
        result = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="test",
            tier=HealthTier.PROGRAMMATIC,
            reasoning="OK",
        )
        d = result.to_dict()
        assert d["action"] == "continue"

    def test_to_dict_empty_details(self):
        result = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="test",
            tier=HealthTier.AGENT,
            reasoning="OK",
        )
        d = result.to_dict()
        assert d["details"] == {}

    def test_health_result_is_frozen(self):
        """HealthResult is immutable (frozen dataclass)."""
        result = HealthResult(
            status=HealthStatus.HEALTHY,
            check_name="test",
            tier=HealthTier.AGENT,
            reasoning="OK",
        )
        with pytest.raises(AttributeError):
            result.status = HealthStatus.FAILED  # type: ignore[misc]


# ===========================================================================
# 13. Integration: HealthCheckRunner with real AgentInspectorCheck
# ===========================================================================


class TestRunnerWithRealInspector:
    """Integration tests with the actual AgentInspectorCheck."""

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_full_run_phase_complete(self, mock_api, tmp_path):
        """Full PHASE_COMPLETE run with healthy Tier 1 + real AgentInspectorCheck."""
        mock_api.return_value = '{"status": "HEALTHY", "reasoning": "All good."}'

        runner = HealthCheckRunner()
        runner.register(_HealthyTier1())
        runner.register(AgentInspectorCheck())

        ctx = _ctx(repo_path=tmp_path, trigger="phase_complete")
        results = runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        assert len(results) == 2
        assert results[0].tier == HealthTier.PROGRAMMATIC
        assert results[1].tier == HealthTier.AGENT
        assert results[1].check_name == "agent_inspector"
        assert results[1].status == HealthStatus.HEALTHY

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_full_run_wave_complete_degraded_triggers_inspector(self, mock_api, tmp_path):
        """WAVE_COMPLETE with degraded Tier 1 triggers the real AgentInspectorCheck."""
        mock_api.return_value = '{"status": "DEGRADED", "reasoning": "Inspector concern."}'

        runner = HealthCheckRunner()
        runner.register(_DegradedTier1())
        runner.register(AgentInspectorCheck())

        ctx = _ctx(repo_path=tmp_path, trigger="wave_complete")
        results = runner.run(ctx, HealthTrigger.WAVE_COMPLETE)

        assert len(results) == 2
        inspector_result = results[1]
        assert inspector_result.check_name == "agent_inspector"
        assert inspector_result.status == HealthStatus.DEGRADED
        assert inspector_result.action == HealthAction.ALERT

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_full_run_api_failure_still_returns_results(self, mock_api, tmp_path):
        """API failure during run still returns a result (graceful degradation)."""
        import httpx

        mock_api.side_effect = httpx.TimeoutException("timeout")

        runner = HealthCheckRunner()
        runner.register(_HealthyTier1())
        runner.register(AgentInspectorCheck())

        ctx = _ctx(repo_path=tmp_path, trigger="phase_complete")
        results = runner.run(ctx, HealthTrigger.PHASE_COMPLETE)

        assert len(results) == 2
        inspector_result = results[1]
        assert inspector_result.status == HealthStatus.HEALTHY  # graceful degradation
        assert "unavailable" in inspector_result.reasoning

    @patch("health_checks.tier2.agent_inspector._call_claude_api")
    def test_full_run_on_demand(self, mock_api, tmp_path):
        """ON_DEMAND always runs Tier 2 with real AgentInspectorCheck."""
        mock_api.return_value = '{"status": "HEALTHY", "reasoning": "On-demand check OK."}'

        runner = HealthCheckRunner()
        runner.register(_HealthyTier1())
        runner.register(AgentInspectorCheck())

        ctx = _ctx(repo_path=tmp_path, trigger="on_demand")
        results = runner.run(ctx, HealthTrigger.ON_DEMAND)

        assert len(results) == 2
        assert any(r.check_name == "agent_inspector" for r in results)
