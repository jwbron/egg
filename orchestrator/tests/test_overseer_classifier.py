"""Tests for overseer classifier functions (Phase 4).

Validates that the Haiku-based classification functions in
``orchestrator/overseer/classifier.py`` correctly build prompts, call
the LLM, parse responses, and cache results.
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

_shared_path = Path(__file__).parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

# Mock docker before importing anything that might transitively need it.
sys.modules.setdefault("docker", MagicMock())
sys.modules.setdefault("docker.errors", MagicMock())
sys.modules.setdefault("docker.types", MagicMock())

# ---------------------------------------------------------------------------
# Conditional import
# ---------------------------------------------------------------------------

try:
    from overseer.classifier import (
        check_alignment,
        classify_error,
        classify_stall,
        clear_cache,
        detect_loop,
    )
except (ImportError, ModuleNotFoundError) as exc:
    pytest.skip(
        f"overseer.classifier not available yet: {exc}",
        allow_module_level=True,
    )

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

try:
    from egg_agent.result import AgentResult
except ImportError:
    from dataclasses import dataclass
    from typing import Any

    @dataclass
    class AgentResult:  # type: ignore[no-redef]
        success: bool
        stdout: str
        stderr: str = ""
        returncode: int = 0
        error: str | None = None
        metadata: dict[str, Any] | None = None
        cost_usd: float | None = None
        num_turns: int | None = None
        duration_ms: int | None = None
        session_id: str | None = None


def _make_result(stdout: str, *, success: bool = True) -> AgentResult:
    """Build a minimal AgentResult for mocking."""
    return AgentResult(
        success=success,
        stdout=stdout,
        stderr="",
        returncode=0 if success else 1,
    )


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


_AGENT_PATCH = "overseer.classifier.run_agent_async"


@pytest.fixture(autouse=True)
def _clear_classifier_cache():
    """Clear the classifier cache before each test."""
    clear_cache()
    yield
    clear_cache()


# ===================================================================
# classify_stall
# ===================================================================


class TestClassifyStallStuck:
    """Test classify_stall returns stuck classification."""

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_classify_stall_stuck(self, mock_agent: AsyncMock) -> None:
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "classification": "stuck",
                    "confidence": 0.9,
                    "reasoning": "No progress for 10 minutes",
                }
            )
        )

        result = _run(
            classify_stall(
                logs=[{"msg": "no output"}],
                progress=[{"state": "idle"}],
            )
        )

        assert result["classification"] == "stuck"
        assert result["confidence"] == 0.9
        assert "reasoning" in result
        mock_agent.assert_awaited_once()


class TestClassifyStallWorking:
    """Test classify_stall returns working classification."""

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_classify_stall_working(self, mock_agent: AsyncMock) -> None:
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "classification": "working",
                    "confidence": 0.85,
                    "reasoning": "Agent is compiling code",
                }
            )
        )

        result = _run(
            classify_stall(
                logs=[{"msg": "compiling..."}],
                progress=[{"state": "working", "step": "build"}],
            )
        )

        assert result["classification"] == "working"
        assert result["confidence"] == 0.85
        mock_agent.assert_awaited_once()


# ===================================================================
# classify_error
# ===================================================================


class TestClassifyErrorSeverity:
    """Test classify_error returns severity levels."""

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_classify_error_severity(self, mock_agent: AsyncMock) -> None:
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "error_type": "oom",
                    "severity": "critical",
                    "recommended_action": "Increase memory limits",
                }
            )
        )

        result = _run(classify_error(error_context={"msg": "OOM killed", "code": 137}))

        assert result["error_type"] == "oom"
        assert result["severity"] == "critical"
        assert "recommended_action" in result
        mock_agent.assert_awaited_once()


# ===================================================================
# detect_loop
# ===================================================================


class TestDetectLoopFound:
    """Test detect_loop when a loop is found."""

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_detect_loop_found(self, mock_agent: AsyncMock) -> None:
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "is_loop": True,
                    "loop_pattern": "edit -> revert -> edit -> revert",
                    "confidence": 0.95,
                }
            )
        )

        result = _run(
            detect_loop(
                recent_actions=[
                    {"action": "edit"},
                    {"action": "revert"},
                    {"action": "edit"},
                    {"action": "revert"},
                ]
            )
        )

        assert result["is_loop"] is True
        assert result["loop_pattern"] is not None
        assert result["confidence"] == 0.95
        mock_agent.assert_awaited_once()


class TestDetectLoopNotFound:
    """Test detect_loop when no loop is found."""

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_detect_loop_not_found(self, mock_agent: AsyncMock) -> None:
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "is_loop": False,
                    "loop_pattern": None,
                    "confidence": 0.9,
                }
            )
        )

        result = _run(
            detect_loop(
                recent_actions=[
                    {"action": "read"},
                    {"action": "edit"},
                    {"action": "test"},
                    {"action": "commit"},
                ]
            )
        )

        assert result["is_loop"] is False
        assert result["loop_pattern"] is None
        mock_agent.assert_awaited_once()


# ===================================================================
# check_alignment
# ===================================================================


class TestCheckAlignmentAligned:
    """Test check_alignment when agent is aligned."""

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_check_alignment_aligned(self, mock_agent: AsyncMock) -> None:
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "aligned": True,
                    "concerns": [],
                    "suggested_redirect": None,
                }
            )
        )

        result = _run(
            check_alignment(
                activity=[{"action": "edit", "file": "auth/views.py"}],
                contract={"task": "fix auth bug", "scope": "auth/"},
            )
        )

        assert result["aligned"] is True
        assert result["concerns"] == []
        assert result["suggested_redirect"] is None
        mock_agent.assert_awaited_once()


# ===================================================================
# Caching
# ===================================================================


class TestClassifierCaching:
    """Test that classifier results are cached."""

    @patch(_AGENT_PATCH, new_callable=AsyncMock)
    def test_classifier_caching(self, mock_agent: AsyncMock) -> None:
        """Same inputs should hit cache; LLM called only once."""
        mock_agent.return_value = _make_result(
            json.dumps(
                {
                    "classification": "stuck",
                    "confidence": 0.9,
                    "reasoning": "No progress",
                }
            )
        )

        logs = [{"msg": "same logs"}]
        progress = [{"state": "idle"}]

        result1 = _run(classify_stall(logs=logs, progress=progress))
        result2 = _run(classify_stall(logs=logs, progress=progress))

        assert result1 == result2
        assert result1["classification"] == "stuck"
        assert mock_agent.await_count == 1, (
            "Expected cached second call -- run_agent_async should be called once"
        )
