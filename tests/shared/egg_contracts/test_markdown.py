"""Tests for the soft-break unwrapper (#3122)."""

from egg_contracts.markdown import unwrap_soft_breaks


class TestUnwrapSoftBreaks:
    def test_empty_and_none(self):
        assert unwrap_soft_breaks(None) == ""
        assert unwrap_soft_breaks("") == ""

    def test_joins_wrapped_paragraph(self):
        text = "This sentence was wrapped by the\nYAML block scalar at an\narbitrary column."
        assert unwrap_soft_breaks(text) == (
            "This sentence was wrapped by the YAML block scalar at an arbitrary column."
        )

    def test_preserves_paragraph_breaks(self):
        text = "First paragraph line one\nline two.\n\nSecond paragraph line one\nline two."
        assert unwrap_soft_breaks(text) == (
            "First paragraph line one line two.\n\nSecond paragraph line one line two."
        )

    def test_preserves_bullet_list_items(self):
        text = "- first item\n- second item\n- third item"
        assert unwrap_soft_breaks(text) == text

    def test_joins_wrapped_bullet_continuation(self):
        text = "- a long bullet that was\n  wrapped onto a second line\n- next bullet"
        assert unwrap_soft_breaks(text) == (
            "- a long bullet that was wrapped onto a second line\n- next bullet"
        )

    def test_preserves_ordered_list_items(self):
        text = "1. first\n2. second\n10. tenth"
        assert unwrap_soft_breaks(text) == text

    def test_preserves_headings(self):
        text = "## Heading\nprose right after the heading\nwrapped once."
        assert unwrap_soft_breaks(text) == (
            "## Heading\nprose right after the heading wrapped once."
        )

    def test_does_not_join_heading_into_prose(self):
        text = "some prose\n## Heading"
        assert unwrap_soft_breaks(text) == text

    def test_preserves_fenced_code(self):
        text = "Run this:\n\n```bash\nmake test\nmake lint\n```\n\nthen push\nand wait."
        assert unwrap_soft_breaks(text) == (
            "Run this:\n\n```bash\nmake test\nmake lint\n```\n\nthen push and wait."
        )

    def test_fence_close_requires_matching_char(self):
        text = "```\n~~~\nstill code\n```\nprose after\nwrapped."
        assert unwrap_soft_breaks(text) == "```\n~~~\nstill code\n```\nprose after wrapped."

    def test_preserves_indented_code(self):
        text = "Example:\n\n    indented code line one\n    indented code line two"
        assert unwrap_soft_breaks(text) == text

    def test_preserves_table(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 |"
        assert unwrap_soft_breaks(text) == text

    def test_preserves_blockquote_markers(self):
        text = "> quoted line one\n> quoted line two"
        assert unwrap_soft_breaks(text) == text

    def test_joins_blockquote_lazy_continuation(self):
        text = "> a quote that was\nwrapped lazily"
        assert unwrap_soft_breaks(text) == "> a quote that was wrapped lazily"

    def test_preserves_hard_break_trailing_spaces(self):
        text = "line with explicit break  \nnext line"
        assert unwrap_soft_breaks(text) == text

    def test_preserves_hard_break_backslash(self):
        text = "line with explicit break\\\nnext line"
        assert unwrap_soft_breaks(text) == text

    def test_preserves_thematic_break(self):
        text = "prose above\n---\nprose below"
        assert unwrap_soft_breaks(text) == text

    def test_preserves_setext_underline(self):
        text = "Heading text\n===\nprose below"
        assert unwrap_soft_breaks(text) == text

    def test_preserves_link_reference_definition(self):
        text = "see the docs\n[docs]: https://example.com"
        assert unwrap_soft_breaks(text) == text

    def test_idempotent(self):
        text = (
            "A wrapped\nparagraph here.\n\n- bullet one\n  wrapped\n- bullet two\n\n"
            "```\ncode\n```\n\n| a |\n| - |\n"
        )
        once = unwrap_soft_breaks(text)
        assert unwrap_soft_breaks(once) == once
