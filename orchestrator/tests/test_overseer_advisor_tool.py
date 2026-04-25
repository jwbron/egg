"""Tests for ``orchestrator.mcp.tools.overseer_advisor`` (issue #1962, TASK-4-1)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from mcp.tools.overseer_advisor import (
    CONSULT_ADVISOR_TOOL,
    handle_consult_advisor,
)

# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------


class TestConsultAdvisorTool:
    def test_tool_name(self) -> None:
        assert CONSULT_ADVISOR_TOOL["name"] == "consult_advisor"

    def test_tool_description_mentions_advisor_and_overseer(self) -> None:
        desc = CONSULT_ADVISOR_TOOL["description"]
        assert "advisor" in desc.lower()
        assert "overseer" in desc.lower()

    def test_input_schema_lists_required(self) -> None:
        schema = CONSULT_ADVISOR_TOOL["inputSchema"]
        assert schema["type"] == "object"
        # classification + health_alerts are required (matches AdvisorVerdict
        # gate inputs); progress_events / recent_log_lines optional.
        assert "classification" in schema["required"]
        assert "health_alerts" in schema["required"]
        assert "progress_events" not in schema["required"]
        assert "recent_log_lines" not in schema["required"]

    def test_input_schema_property_types(self) -> None:
        props = CONSULT_ADVISOR_TOOL["inputSchema"]["properties"]
        assert props["classification"]["type"] == "object"
        assert props["health_alerts"]["type"] == "array"
        assert props["progress_events"]["type"] == "array"
        assert props["recent_log_lines"]["type"] == "array"
        assert props["recent_log_lines"]["items"]["type"] == "string"


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


class TestAuthGate:
    @pytest.mark.parametrize(
        "role",
        ["coder", "tester", "documenter", "reviewer_code", "", None],
    )
    def test_non_overseer_role_returns_error(self, role: str | None) -> None:
        result = asyncio.run(
            handle_consult_advisor(
                classification={"type": "x"},
                health_alerts=[{"type": "h"}],
                role=role,
            )
        )
        assert result == {
            "ok": False,
            "error": (
                f"consult_advisor: only the 'overseer' role may call this tool (got role={role!r})"
            ),
        }

    def test_overseer_role_passes_through(self) -> None:
        async def fake_consult_advisor(**kwargs: Any) -> Any:
            from egg_overseer.advisor import AdvisorVerdict

            return AdvisorVerdict(decision="watch", reasoning="r")

        with patch("egg_overseer.advisor.consult_advisor", side_effect=fake_consult_advisor):
            result = asyncio.run(
                handle_consult_advisor(
                    classification={"type": "x"},
                    health_alerts=[{"type": "h"}],
                    role="overseer",
                )
            )
        assert result["ok"] is True
        assert result["verdict"]["decision"] == "watch"

    def test_overseer_case_insensitive(self) -> None:
        async def fake_consult_advisor(**kwargs: Any) -> Any:
            from egg_overseer.advisor import AdvisorVerdict

            return AdvisorVerdict(decision="watch", reasoning="r")

        with patch("egg_overseer.advisor.consult_advisor", side_effect=fake_consult_advisor):
            result = asyncio.run(
                handle_consult_advisor(
                    classification={},
                    health_alerts=[],
                    role="OVERSEER",
                )
            )
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# Handler dispatch + verdict serialization
# ---------------------------------------------------------------------------


class TestHandlerDispatch:
    def test_returns_serialized_verdict(self) -> None:
        from egg_overseer.advisor import AdvisorVerdict

        async def fake_consult_advisor(**kwargs: Any) -> AdvisorVerdict:
            return AdvisorVerdict(
                decision="alert",
                priority="p1",
                alert_summary="stall",
                alert_detail="5 min",
                reasoning="tier1+haiku tripped",
            )

        with patch("egg_overseer.advisor.consult_advisor", side_effect=fake_consult_advisor):
            result = asyncio.run(
                handle_consult_advisor(
                    classification={"type": "x"},
                    health_alerts=[{"type": "h"}],
                    role="overseer",
                )
            )
        assert result["ok"] is True
        assert result["verdict"]["decision"] == "alert"
        assert result["verdict"]["priority"] == "p1"
        assert result["verdict"]["alert_summary"] == "stall"

    def test_default_optional_lists_passed_through(self) -> None:
        from egg_overseer.advisor import AdvisorVerdict

        captured: dict[str, Any] = {}

        async def fake_consult_advisor(**kwargs: Any) -> AdvisorVerdict:
            captured.update(kwargs)
            return AdvisorVerdict(decision="watch", reasoning="r")

        with patch("egg_overseer.advisor.consult_advisor", side_effect=fake_consult_advisor):
            asyncio.run(
                handle_consult_advisor(
                    classification={"type": "x"},
                    health_alerts=[{"type": "h"}],
                    role="overseer",
                )
            )
        assert captured["progress_events"] == []
        assert captured["recent_log_lines"] == []

    def test_parse_failure_returns_error_dict(self) -> None:
        from egg_overseer.advisor import AdvisorParseError

        async def fake_consult_advisor(**kwargs: Any) -> Any:
            raise AdvisorParseError("bad json")

        with patch("egg_overseer.advisor.consult_advisor", side_effect=fake_consult_advisor):
            result = asyncio.run(
                handle_consult_advisor(
                    classification={"type": "x"},
                    health_alerts=[{"type": "h"}],
                    role="overseer",
                )
            )
        assert result["ok"] is False
        assert "parse_failure" in result["error"]
        assert "bad json" in result["error"]

    def test_config_forwarded(self) -> None:
        from egg_overseer.advisor import AdvisorVerdict

        captured: dict[str, Any] = {}

        async def fake_consult_advisor(**kwargs: Any) -> AdvisorVerdict:
            captured.update(kwargs)
            return AdvisorVerdict(decision="watch", reasoning="r")

        sentinel_config = object()
        with patch("egg_overseer.advisor.consult_advisor", side_effect=fake_consult_advisor):
            asyncio.run(
                handle_consult_advisor(
                    classification={},
                    health_alerts=[],
                    role="overseer",
                    config=sentinel_config,
                )
            )
        assert captured["config"] is sentinel_config
