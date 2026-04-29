"""Tests for ``egg_overseer.advisor`` (issue #1962, TASK-4-1).

Covers the AdvisorVerdict validator, the JSON parser, the secret-scrub
defense-in-depth pass on file_issue payloads, and the test seam
``_agent_runner``.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from egg_overseer.advisor import (
    _DEFAULT_RECENT_LOG_BYTES_CAP,
    AdvisorParseError,
    AdvisorVerdict,
    _truncate_log_lines_by_bytes,
    consult_advisor,
)

_GH_PAT = "ghp_" + "A" * 36


# ---------------------------------------------------------------------------
# _truncate_log_lines_by_bytes (issue #2120)
# ---------------------------------------------------------------------------


class TestTruncateLogLinesByBytes:
    def test_under_cap_returns_input_unchanged(self) -> None:
        lines = ["a", "b", "c"]
        kept, dropped, dropped_bytes = _truncate_log_lines_by_bytes(lines, cap_bytes=1000)
        assert kept == lines
        assert dropped == 0
        assert dropped_bytes == 0

    def test_drops_oldest_first(self) -> None:
        lines = ["aaaa", "bbbb", "cccc"]  # each line + newline = 5 bytes
        # Cap of 11 bytes fits the last 2 (cccc + newline = 5, bbbb + newline = 5, total 10).
        kept, dropped, dropped_bytes = _truncate_log_lines_by_bytes(lines, cap_bytes=11)
        assert kept == ["bbbb", "cccc"]
        assert dropped == 1
        assert dropped_bytes == 5  # 4 bytes for "aaaa" + 1 for newline

    def test_zero_cap_disables(self) -> None:
        lines = ["a", "b"]
        kept, dropped, dropped_bytes = _truncate_log_lines_by_bytes(lines, cap_bytes=0)
        assert kept == lines
        assert dropped == 0
        assert dropped_bytes == 0

    def test_negative_cap_disables(self) -> None:
        lines = ["a", "b"]
        kept, dropped, _ = _truncate_log_lines_by_bytes(lines, cap_bytes=-1)
        assert kept == lines
        assert dropped == 0

    def test_empty_input_safe(self) -> None:
        kept, dropped, dropped_bytes = _truncate_log_lines_by_bytes([], cap_bytes=10)
        assert kept == []
        assert dropped == 0
        assert dropped_bytes == 0

    def test_single_line_larger_than_cap_drops_all(self) -> None:
        # A pathologically long single line (the headline failure mode
        # in issue #2120) is dropped along with everything before it.
        # The caller renders a marker so the advisor sees an explicit
        # "(none)"-with-marker rather than tripping a context-window
        # overflow downstream.
        lines = ["short", "x" * 1000]
        kept, dropped, _ = _truncate_log_lines_by_bytes(lines, cap_bytes=100)
        assert kept == []
        assert dropped == 2

    def test_utf8_bytes_not_codepoints(self) -> None:
        # Multi-byte UTF-8 characters must count as bytes, not codepoints.
        # "🔥" is 4 UTF-8 bytes; cap of 8 should fit only one such line
        # (4 bytes + 1 newline = 5, two such lines = 10 > 8).
        lines = ["🔥", "🔥"]
        kept, dropped, _ = _truncate_log_lines_by_bytes(lines, cap_bytes=8)
        assert kept == ["🔥"]
        assert dropped == 1

    def test_default_cap_constant_is_sane(self) -> None:
        # Sanity guard: opus context window (~200k tokens, ~600+ KB at
        # 3 bytes/token avg). Default sits well below that with headroom.
        assert _DEFAULT_RECENT_LOG_BYTES_CAP > 0
        assert _DEFAULT_RECENT_LOG_BYTES_CAP <= 1_000_000


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

    def test_alert_requires_priority(self) -> None:
        # Reviewer-flagged validator gap: ``label_to_alert`` crashes on
        # priority=None for ``decision=alert``. Catch it at parse time.
        with pytest.raises(ValueError, match="priority"):
            AdvisorVerdict(
                decision="alert",
                alert_summary="something fired",
                reasoning="r",
            )

    def test_alert_with_summary_and_priority_validates(self) -> None:
        v = AdvisorVerdict(
            decision="alert",
            alert_summary="something fired",
            priority="p2",
            reasoning="r",
        )
        assert v.alert_summary == "something fired"
        assert v.priority == "p2"

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

    def test_bare_object_only_payload(self) -> None:
        # A pure bare JSON object — the most common well-behaved shape.
        runner = self._runner_returning('{"decision": "watch", "reasoning": "r"}')
        verdict = asyncio.run(
            consult_advisor(
                classification={},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=[],
                _agent_runner=runner,
            )
        )
        assert verdict.decision == "watch"

    def test_fenced_block_payload(self) -> None:
        # ```json … ``` wrapper — defensive fence-strip path.
        runner = self._runner_returning('```json\n{"decision": "watch", "reasoning": "r"}\n```')
        verdict = asyncio.run(
            consult_advisor(
                classification={},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=[],
                _agent_runner=runner,
            )
        )
        assert verdict.decision == "watch"

    def test_prose_with_bare_object_payload(self) -> None:
        # Prose-then-bare-JSON: the carry-forward case from #2096 review.
        runner = self._runner_returning(
            'Here is my verdict:\n{"decision": "watch", "reasoning": "r"}\n'
        )
        verdict = asyncio.run(
            consult_advisor(
                classification={},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=[],
                _agent_runner=runner,
            )
        )
        assert verdict.decision == "watch"

    def test_prose_around_fenced_payload(self) -> None:
        # Fence in the middle of prose (startswith check misses it,
        # raw_decode fall-back catches it).
        runner = self._runner_returning(
            "Looking at the signals:\n"
            '```json\n{"decision": "watch", "reasoning": "r"}\n```\n'
            "That is my call."
        )
        verdict = asyncio.run(
            consult_advisor(
                classification={},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=[],
                _agent_runner=runner,
            )
        )
        assert verdict.decision == "watch"

    def test_no_brace_raises_parse_error(self) -> None:
        # Confirms the fall-back doesn't paper over genuinely empty
        # responses — error semantics for unparseable text stay intact.
        runner = self._runner_returning("absolutely no json here")
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

    def test_embedded_brace_in_string_value(self) -> None:
        # Locks the string-aware contract: ``raw_decode`` must stop at
        # the closing ``}`` of the JSON object even when value strings
        # contain a literal ``}``. A naive brace-counting scan would
        # truncate the payload and break validation.
        runner = self._runner_returning(
            'Verdict:\n{"decision": "watch", "reasoning": "value with } embedded brace"}'
        )
        verdict = asyncio.run(
            consult_advisor(
                classification={},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=[],
                _agent_runner=runner,
            )
        )
        assert verdict.decision == "watch"
        assert verdict.reasoning == "value with } embedded brace"

    def test_trailing_prose_after_payload(self) -> None:
        # ``raw_decode`` accepts trailing data after a complete JSON
        # value, so unfenced prose tacked on the end of a verdict is
        # tolerated even though plain ``json.loads`` rejects it with
        # "Extra data".
        runner = self._runner_returning('{"decision": "watch", "reasoning": "r"} thanks!')
        verdict = asyncio.run(
            consult_advisor(
                classification={},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=[],
                _agent_runner=runner,
            )
        )
        assert verdict.decision == "watch"

    def test_stray_leading_brace_skipped(self) -> None:
        # Leading prose contains a ``{`` that is NOT the start of the
        # verdict (e.g. a templated placeholder). The loop must skip
        # past the unparseable snippet and find the real JSON object.
        runner = self._runner_returning(
            'see {field_name} below: {"decision": "watch", "reasoning": "r"}'
        )
        verdict = asyncio.run(
            consult_advisor(
                classification={},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=[],
                _agent_runner=runner,
            )
        )
        assert verdict.decision == "watch"

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

    def test_invalid_json_error_message_is_scrubbed(self) -> None:
        # Raw model output is embedded in the AdvisorParseError message
        # and ends up in stderr via cmd_overseer_consult_advisor. If the
        # model parrots a credential in its prose, the error must not
        # surface it verbatim.
        runner = self._runner_returning(f"oops, leaked {_GH_PAT} not json")
        with pytest.raises(AdvisorParseError) as excinfo:
            asyncio.run(
                consult_advisor(
                    classification={},
                    health_alerts=[],
                    progress_events=[],
                    recent_log_lines=[],
                    _agent_runner=runner,
                )
            )
        message = str(excinfo.value)
        assert _GH_PAT not in message
        assert "[REDACTED:gh-pat]" in message

    def test_validation_error_message_is_scrubbed(self) -> None:
        # Same concern on the schema-validation path: the payload repr
        # and pydantic error both echo input values back into the
        # message string.
        runner = self._runner_returning(
            {"decision": "alert", "alert_summary": f"saw token {_GH_PAT}"}
        )
        with pytest.raises(AdvisorParseError) as excinfo:
            asyncio.run(
                consult_advisor(
                    classification={},
                    health_alerts=[],
                    progress_events=[],
                    recent_log_lines=[],
                    _agent_runner=runner,
                )
            )
        message = str(excinfo.value)
        assert _GH_PAT not in message
        assert "[REDACTED:gh-pat]" in message

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

    def test_config_without_model_attr_falls_back_to_opus(self) -> None:
        """A duck-typed config that omits ``overseer_advisor_model``
        (e.g. a ``SimpleNamespace`` assembled from a partial status
        payload that only carries the bytes-cap field) must not raise
        ``AttributeError`` on the model lookup — the resolver uses
        ``getattr`` with the ``"opus"`` fallback for symmetry with the
        bytes-cap field's defensive ``getattr``.
        """
        seen: dict[str, str] = {}

        class _PartialConf:
            overseer_advisor_recent_log_bytes_cap = 0  # cap-only payload

        async def runner(prompt: str, model: str) -> str:
            seen["model"] = model
            return json.dumps({"decision": "watch", "reasoning": "r"})

        asyncio.run(
            consult_advisor(
                classification={},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=[],
                config=_PartialConf(),  # type: ignore[arg-type]
                _agent_runner=runner,
            )
        )
        assert seen["model"] == "opus"

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

    def test_recent_log_bytes_cap_arg_overrides_default(self) -> None:
        # Smaller cap → truncation marker present in prompt.
        captured: dict[str, str] = {}

        async def runner(prompt: str, model: str) -> str:
            captured["prompt"] = prompt
            return json.dumps({"decision": "watch", "reasoning": "r"})

        # Each line is ~20 bytes; cap of 50 bytes keeps ~2 most-recent lines.
        lines = [f"line-{i:02d} payload" for i in range(20)]
        asyncio.run(
            consult_advisor(
                classification={"type": "x"},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=lines,
                recent_log_bytes_cap=50,
                _agent_runner=runner,
            )
        )
        prompt = captured["prompt"]
        assert "earlier line(s) dropped" in prompt
        # Last line must survive; oldest lines must be gone.
        assert "line-19 payload" in prompt
        assert "line-00 payload" not in prompt

    def test_recent_log_bytes_cap_zero_disables(self) -> None:
        captured: dict[str, str] = {}

        async def runner(prompt: str, model: str) -> str:
            captured["prompt"] = prompt
            return json.dumps({"decision": "watch", "reasoning": "r"})

        lines = [f"line-{i:02d}" for i in range(10)]
        asyncio.run(
            consult_advisor(
                classification={"type": "x"},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=lines,
                recent_log_bytes_cap=0,
                _agent_runner=runner,
            )
        )
        prompt = captured["prompt"]
        assert "earlier line(s) dropped" not in prompt
        assert "line-00" in prompt
        assert "line-09" in prompt

    def test_default_cap_does_not_fire_for_small_inputs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No truncation on a typical-size payload — default cap holds.
        async def runner(prompt: str, model: str) -> str:
            return json.dumps({"decision": "watch", "reasoning": "r"})

        with caplog.at_level(logging.INFO, logger="egg_overseer.advisor"):
            asyncio.run(
                consult_advisor(
                    classification={"type": "x"},
                    health_alerts=[],
                    progress_events=[],
                    recent_log_lines=["short line"] * 50,
                    _agent_runner=runner,
                )
            )
        truncation_events = [
            r for r in caplog.records if getattr(r, "event", None) == "advisor_log_truncated"
        ]
        assert truncation_events == []

    def test_truncation_emits_metric_log_event(self, caplog: pytest.LogCaptureFixture) -> None:
        async def runner(prompt: str, model: str) -> str:
            return json.dumps({"decision": "watch", "reasoning": "r"})

        lines = [f"line-{i:02d} payload" for i in range(20)]
        with caplog.at_level(logging.INFO, logger="egg_overseer.advisor"):
            asyncio.run(
                consult_advisor(
                    classification={"type": "x"},
                    health_alerts=[],
                    progress_events=[],
                    recent_log_lines=lines,
                    recent_log_bytes_cap=50,
                    _agent_runner=runner,
                )
            )
        truncation_events = [
            r for r in caplog.records if getattr(r, "event", None) == "advisor_log_truncated"
        ]
        assert len(truncation_events) == 1
        rec = truncation_events[0]
        assert rec.dropped_lines > 0  # type: ignore[attr-defined]
        assert rec.dropped_bytes > 0  # type: ignore[attr-defined]
        assert rec.cap_bytes == 50  # type: ignore[attr-defined]
        assert rec.input_line_count == 20  # type: ignore[attr-defined]

    def test_pathological_single_line_drops_everything(self) -> None:
        # A single most-recent line larger than the cap is dropped along
        # with everything before it; the section falls through to "(none)"
        # under a marker so the advisor sees the truncation explicitly
        # instead of an SDK error from a context-window overflow.
        captured: dict[str, str] = {}

        async def runner(prompt: str, model: str) -> str:
            captured["prompt"] = prompt
            return json.dumps({"decision": "watch", "reasoning": "r"})

        huge = "x" * 1000
        asyncio.run(
            consult_advisor(
                classification={"type": "x"},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=["short", huge],
                recent_log_bytes_cap=100,
                _agent_runner=runner,
            )
        )
        prompt = captured["prompt"]
        assert "earlier line(s) dropped" in prompt
        assert huge not in prompt

    def test_config_field_used_when_arg_omitted(self) -> None:
        captured: dict[str, str] = {}

        async def runner(prompt: str, model: str) -> str:
            captured["prompt"] = prompt
            return json.dumps({"decision": "watch", "reasoning": "r"})

        class _Conf:
            overseer_advisor_model = "opus"
            overseer_advisor_recent_log_bytes_cap = 50

        lines = [f"line-{i:02d} payload" for i in range(20)]
        asyncio.run(
            consult_advisor(
                classification={"type": "x"},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=lines,
                config=_Conf(),  # type: ignore[arg-type]
                _agent_runner=runner,
            )
        )
        assert "earlier line(s) dropped" in captured["prompt"]

    def test_explicit_arg_overrides_config_field(self) -> None:
        captured: dict[str, str] = {}

        async def runner(prompt: str, model: str) -> str:
            captured["prompt"] = prompt
            return json.dumps({"decision": "watch", "reasoning": "r"})

        class _Conf:
            overseer_advisor_model = "opus"
            # Config says "tight cap" but explicit arg disables it.
            overseer_advisor_recent_log_bytes_cap = 50

        lines = [f"line-{i:02d}" for i in range(10)]
        asyncio.run(
            consult_advisor(
                classification={"type": "x"},
                health_alerts=[],
                progress_events=[],
                recent_log_lines=lines,
                config=_Conf(),  # type: ignore[arg-type]
                recent_log_bytes_cap=0,
                _agent_runner=runner,
            )
        )
        assert "earlier line(s) dropped" not in captured["prompt"]
        assert "line-00" in captured["prompt"]

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
