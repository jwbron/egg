"""Tests for ``egg_overseer.advisor`` (issue #1962, TASK-4-1).

Covers the AdvisorVerdict validator, the JSON parser, the secret-scrub
defense-in-depth pass on file_issue payloads, and the test seam
``_agent_runner``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from egg_overseer.advisor import (
    AdvisorParseError,
    AdvisorVerdict,
    consult_advisor,
)

_GH_PAT = "ghp_" + "A" * 36


# ---------------------------------------------------------------------------
# AdvisorVerdict validator
# ---------------------------------------------------------------------------


class TestAdvisorVerdictValidator:
    def test_watch_minimum_payload(self) -> None:
        v = AdvisorVerdict(decision="watch", reasoning="quiet cycle")
        assert v.decision == "watch"
        assert v.priority is None

    def test_alert_requires_summary(self) -> None:
        with pytest.raises(ValueError, match="alert_summary"):
            AdvisorVerdict(decision="alert", reasoning="trip")

    def test_alert_with_summary_validates(self) -> None:
        v = AdvisorVerdict(
            decision="alert",
            alert_summary="something fired",
            reasoning="r",
        )
        assert v.alert_summary == "something fired"

    def test_file_issue_requires_title_and_body(self) -> None:
        with pytest.raises(ValueError, match="issue_title"):
            AdvisorVerdict(
                decision="file_issue",
                priority="p1",
                issue_body="body only",
                reasoning="r",
            )
        with pytest.raises(ValueError, match="issue_body"):
            AdvisorVerdict(
                decision="file_issue",
                priority="p1",
                issue_title="title only",
                reasoning="r",
            )

    def test_file_issue_requires_priority(self) -> None:
        with pytest.raises(ValueError, match="priority"):
            AdvisorVerdict(
                decision="file_issue",
                issue_title="t",
                issue_body="b",
                reasoning="r",
            )

    def test_file_issue_full_payload(self) -> None:
        v = AdvisorVerdict(
            decision="file_issue",
            priority="p1",
            issue_title="t",
            issue_body="b",
            reasoning="r",
        )
        assert v.priority == "p1"

    def test_invalid_decision_rejected(self) -> None:
        with pytest.raises(ValueError):
            AdvisorVerdict(decision="ignore", reasoning="r")  # type: ignore[arg-type]

    def test_invalid_priority_rejected(self) -> None:
        with pytest.raises(ValueError):
            AdvisorVerdict(
                decision="alert",
                alert_summary="x",
                priority="critical",  # type: ignore[arg-type]
                reasoning="r",
            )


# ---------------------------------------------------------------------------
# consult_advisor (uses _agent_runner test seam)
# ---------------------------------------------------------------------------


class TestConsultAdvisor:
    def _runner_returning(self, payload: dict | str):
        async def _runner(prompt: str, model: str) -> str:
            if isinstance(payload, str):
                return payload
            return json.dumps(payload)

        return _runner

    def test_returns_validated_watch_verdict(self) -> None:
        runner = self._runner_returning({"decision": "watch", "reasoning": "nothing to do"})
        verdict = asyncio.run(
            consult_advisor(
                classification={"type": "agent-stall", "confidence": 0.9},
                health_alerts=[{"type": "tier1_health"}],
                progress_events=[],
                recent_log_lines=[],
                _agent_runner=runner,
            )
        )
        assert verdict.decision == "watch"

    def test_returns_validated_alert_verdict(self) -> None:
        runner = self._runner_returning(
            {
                "decision": "alert",
                "priority": "p1",
                "alert_summary": "stall",
                "alert_detail": "5 min",
                "reasoning": "tripped",
            }
        )
        verdict = asyncio.run(
            consult_advisor(
                classification={"type": "x"},
                health_alerts=[{"type": "y"}],
                progress_events=[],
                recent_log_lines=[],
                _agent_runner=runner,
            )
        )
        assert verdict.decision == "alert"
        assert verdict.priority == "p1"

    def test_file_issue_body_is_scrubbed_defense_in_depth(self) -> None:
        # The advisor is the primary scrubber but consult_advisor runs
        # one final pass before returning.
        body = f"Token leaked: {_GH_PAT}\nPlease investigate."
        runner = self._runner_returning(
            {
                "decision": "file_issue",
                "priority": "p1",
                "issue_title": "title",
                "issue_body": body,
                "reasoning": "r",
            }
        )
        verdict = asyncio.run(
            consult_advisor(
                classification={"type": "x"},
                health_alerts=[{"type": "y"}],
                progress_events=[],
                recent_log_lines=[],
                _agent_runner=runner,
            )
        )
        assert verdict.decision == "file_issue"
        assert verdict.issue_body is not None
        assert _GH_PAT not in verdict.issue_body
        assert "[REDACTED:gh-pat]" in verdict.issue_body

    def test_invalid_json_raises_parse_error(self) -> None:
        runner = self._runner_returning("this is not json")
        with pytest.raises(AdvisorParseError, match="not valid JSON"):
            asyncio.run(
                consult_advisor(
                    classification={},
                    health_alerts=[],
                    progress_events=[],
                    recent_log_lines=[],
                    _agent_runner=runner,
                )
            )

    def test_schema_failure_raises_parse_error(self) -> None:
        runner = self._runner_returning({"decision": "alert"})  # missing reasoning
        with pytest.raises(AdvisorParseError, match="validation"):
            asyncio.run(
                consult_advisor(
                    classification={},
                    health_alerts=[],
                    progress_events=[],
                    recent_log_lines=[],
                    _agent_runner=runner,
                )
            )

    def test_default_model_used_when_config_none(self) -> None:
        seen: dict[str, str] = {}

        async def runner(prompt: str, model: str) -> str:
            seen["model"] = model
            return json.dumps({"decision": "watch", "reasoning": "r"})

        asyncio.run(
            consult_advisor(
                classification={},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=[],
                _agent_runner=runner,
            )
        )
        assert seen["model"] == "opus"

    def test_config_model_override(self) -> None:
        seen: dict[str, str] = {}

        class _Conf:
            overseer_advisor_model = "claude-opus-4-7"

        async def runner(prompt: str, model: str) -> str:
            seen["model"] = model
            return json.dumps({"decision": "watch", "reasoning": "r"})

        asyncio.run(
            consult_advisor(
                classification={},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=[],
                config=_Conf(),  # type: ignore[arg-type]
                _agent_runner=runner,
            )
        )
        assert seen["model"] == "claude-opus-4-7"

    def test_prompt_includes_distilled_summary_sections(self) -> None:
        captured: dict[str, str] = {}

        async def runner(prompt: str, model: str) -> str:
            captured["prompt"] = prompt
            return json.dumps({"decision": "watch", "reasoning": "r"})

        asyncio.run(
            consult_advisor(
                classification={"type": "agent-stall", "confidence": 0.9},
                health_alerts=[{"type": "h1", "severity": "tier1"}],
                progress_events=[{"role": "coder", "event": "start"}],
                recent_log_lines=["log line 1", "log line 2"],
                _agent_runner=runner,
            )
        )
        prompt = captured["prompt"]
        # The decision-20 opt-3 contract requires four named sections.
        assert "Haiku classification" in prompt
        assert "Tier-1 health alerts" in prompt
        assert "progress events" in prompt
        assert "container log lines" in prompt
        assert "AdvisorVerdict" in prompt

    def test_prompt_handles_empty_lists_explicitly(self) -> None:
        captured: dict[str, str] = {}

        async def runner(prompt: str, model: str) -> str:
            captured["prompt"] = prompt
            return json.dumps({"decision": "watch", "reasoning": "r"})

        asyncio.run(
            consult_advisor(
                classification={"type": "x"},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=[],
                _agent_runner=runner,
            )
        )
        # Each empty section renders as "(none)" so the advisor reads
        # "no alerts" rather than guessing the schema.
        assert captured["prompt"].count("(none)") >= 3
