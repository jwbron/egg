"""Tests for ``egg_overseer.issue_template`` (issue #1962, decision-8 opt-2)."""

from __future__ import annotations

from egg_overseer.issue_template import TEMPLATE_LITERAL, render


class TestTemplateLiteral:
    def test_literal_is_non_empty_string(self) -> None:
        assert isinstance(TEMPLATE_LITERAL, str)
        assert TEMPLATE_LITERAL.strip()

    def test_literal_contains_expected_sections(self) -> None:
        # Each "## ###" section the planner specified must be present
        # so future drift fails loudly.
        for section in (
            "## Pipeline Diagnostic:",
            "### Anomaly",
            "### Timeline",
            "### Classification",
            "### Actions Taken",
            "### Suggested Remediation",
            "### Pipeline Links",
        ):
            assert section in TEMPLATE_LITERAL, f"missing section {section!r}"

    def test_literal_carries_pipeline_links_subblock(self) -> None:
        # decision-8 opt-2: extend the existing template with explicit
        # links. All five sub-block fields must be present.
        for placeholder in (
            "{pipeline_id}",
            "{phase}",
            "{branch}",
            "{commit_sha}",
            "{parent_alert_message_id}",
        ):
            assert placeholder in TEMPLATE_LITERAL


class TestRender:
    def _fields(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "anomaly_type": "agent-stall",
            "pipeline_id": "issue-9999",
            "phase": "implement",
            "agent_role": "coder",
            "timestamp": "2026-04-25T20:00:00Z",
            "anomaly_description": "Coder has been stalled for 5 minutes.",
            "timeline_lines": "- 20:00:00: stall detected",
            "classification_lines": "- Type: agent-stall",
            "actions_lines": "- None",
            "container_logs_section": "",
            "remediation": "Investigate logs.",
            "branch_url": "https://github.com/owner/repo/tree/branch",
            "branch": "egg/issue-9999",
            "commit_sha": "abc1234",
            "parent_alert_message_id": "msg-123",
        }
        base.update(overrides)
        return base

    def test_render_substitutes_all_fields(self) -> None:
        body = render(**self._fields())
        # Render output must NOT carry literal {placeholder} markers.
        assert "{pipeline_id}" not in body
        assert "{anomaly_type}" not in body
        # Pipeline Links sub-block fields must be substituted.
        assert "issue-9999" in body
        assert "implement" in body
        assert "egg/issue-9999" in body
        assert "abc1234" in body
        assert "msg-123" in body

    def test_render_preserves_section_order(self) -> None:
        body = render(**self._fields())
        idx_anomaly = body.find("### Anomaly")
        idx_timeline = body.find("### Timeline")
        idx_classification = body.find("### Classification")
        idx_actions = body.find("### Actions Taken")
        idx_remediation = body.find("### Suggested Remediation")
        idx_links = body.find("### Pipeline Links")
        assert -1 < idx_anomaly < idx_timeline < idx_classification < idx_actions
        assert idx_actions < idx_remediation < idx_links

    def test_render_with_empty_container_logs_section(self) -> None:
        # container_logs_section is conditional — when empty the body
        # should still render cleanly without leaving a stray heading.
        body = render(**self._fields(container_logs_section=""))
        assert "Container Logs" not in body

    def test_render_with_filled_container_logs_section(self) -> None:
        logs = "\n\n### Container Logs\n````\noom-killer\n````\n"
        body = render(**self._fields(container_logs_section=logs))
        assert "### Container Logs" in body
        assert "oom-killer" in body
