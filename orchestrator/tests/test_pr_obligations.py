"""Unit tests for ``orchestrator.pr_obligations`` (#2354).

The renderer is shared between the legacy ``_auto_create_pr`` path
(``routes/pipelines.py``) and the slice-DAG umbrella PR path
(``gateway_client.create_slice_pr``). These tests pin the markdown
shape so neither caller can drift independently of the other.
"""

from __future__ import annotations

import sys
from pathlib import Path

_orchestrator_path = Path(__file__).parent.parent
if str(_orchestrator_path) not in sys.path:
    sys.path.insert(0, str(_orchestrator_path))

from pr_obligations import (
    normalize_deferred_actions,
    render_obligations_section,
    render_obligations_section_from_normalized,
)


# Lightweight stand-in for ``DeferredAction`` to avoid importing the
# full pydantic model in a unit test (the renderer reads via
# ``getattr`` so any object with the three attrs works).
class _DA:
    def __init__(
        self,
        condition: str,
        reviewer: str = "",
        resolved_in_diff: str = "",
    ) -> None:
        self.condition = condition
        self.reviewer = reviewer
        self.resolved_in_diff = resolved_in_diff


class TestRenderObligationsSection:
    def test_empty_input_returns_empty_string(self) -> None:
        assert render_obligations_section(None) == ""
        assert render_obligations_section([]) == ""

    def test_open_obligation_renders_merge_blocking_banner(self) -> None:
        section = render_obligations_section(
            [_DA(condition="git mv legacy/x new/x before merge", reviewer="coder")]
        )
        assert "## ⚠️ Pre-merge Obligations" in section
        assert "Do **not** merge this PR until every obligation is complete" in section
        assert "- **coder** — git mv legacy/x new/x before merge" in section
        # No resolved-in-diff section when nothing is resolved.
        assert "Resolved within this PR" not in section

    def test_resolved_obligation_renders_under_resolved_subsection(self) -> None:
        section = render_obligations_section(
            [
                _DA(
                    condition="verify make test-all green against merged state",
                    reviewer="reviewer_contract",
                    resolved_in_diff="2c319626a",
                )
            ]
        )
        # No merge-blocking banner when *only* resolved obligations are present.
        assert "## ⚠️ Pre-merge Obligations" not in section
        assert "## ✅ Resolved within this PR" in section
        assert "no merge action required" in section
        assert "- **reviewer_contract** — verify make test-all green" in section
        # Bare SHA (not in backticks) so GitHub auto-links it.
        assert "Resolved in 2c319626a" in section
        assert "`2c319626a`" not in section

    def test_mixed_open_and_resolved_renders_both_sections(self) -> None:
        section = render_obligations_section(
            [
                _DA(condition="open work", reviewer="r1"),
                _DA(condition="closed work", reviewer="r2", resolved_in_diff="abc123"),
            ]
        )
        # Both banners present; open first (merge-blocking, more urgent).
        assert section.index("## ⚠️ Pre-merge Obligations") < section.index(
            "## ✅ Resolved within this PR"
        )
        assert "- **r1** — open work" in section
        assert "- **r2** — closed work" in section
        assert "Resolved in abc123" in section

    def test_legacy_string_entry_is_parsed(self) -> None:
        section = render_obligations_section(["coder: git mv legacy/x new/x before merge"])
        assert "- **coder** — git mv legacy/x new/x before merge" in section

    def test_legacy_string_without_reviewer_falls_back_to_unknown(self) -> None:
        section = render_obligations_section(["bare condition with no reviewer"])
        assert "- **unknown** — bare condition with no reviewer" in section

    def test_whitespace_only_condition_is_dropped(self) -> None:
        # All inputs whitespace-only → no obligations → empty section.
        assert render_obligations_section([_DA(condition="   ", reviewer="r1")]) == ""
        assert render_obligations_section(["   "]) == ""

    def test_multiline_condition_indents_continuation_lines(self) -> None:
        section = render_obligations_section(
            [_DA(condition="line one\nline two\nline three", reviewer="r1")]
        )
        assert "- **r1** — line one" in section
        assert "  line two" in section
        assert "  line three" in section


class TestNormalizeDeferredActions:
    def test_strips_whitespace_around_fields(self) -> None:
        normalized = normalize_deferred_actions(
            [_DA(condition="  do X  ", reviewer="  r1  ", resolved_in_diff=" abc ")]
        )
        assert normalized == [{"reviewer": "r1", "condition": "do X", "resolved_in_diff": "abc"}]

    def test_legacy_str_with_reviewer_prefix_is_split(self) -> None:
        normalized = normalize_deferred_actions(["coder: do X"])
        assert normalized == [{"reviewer": "coder", "condition": "do X", "resolved_in_diff": ""}]


class TestRenderFromNormalized:
    def test_directly_consumable_dict_input(self) -> None:
        section = render_obligations_section_from_normalized(
            [{"reviewer": "r1", "condition": "do X", "resolved_in_diff": ""}]
        )
        assert "- **r1** — do X" in section

    def test_empty_normalized_list_returns_empty_string(self) -> None:
        assert render_obligations_section_from_normalized([]) == ""
