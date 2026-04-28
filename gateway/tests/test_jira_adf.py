"""
Tests for ``gateway/jira_adf.py``.

Covers:

- ``wrap_text_as_adf`` produces the minimal ADF doc shape the Atlassian REST
  API v3 expects for ``description`` / comment ``body`` fields.
- Plain-text wrap, empty-string wrap, multi-line wrap (newline-separated
  paragraph nodes), blank-line preservation.
- ``None`` and non-string inputs are coerced (defensive behaviour — callers
  are expected to pass strings, but a ``str()`` fallback prevents a crash if
  a misbehaving caller hands us an int / Path / etc.).
- ``is_adf_dict`` returns True for Atlassian-style ADF samples and rejects
  plain dicts, lists, strings, None, and dicts missing required keys / with
  wrong key types.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# Modules loaded via conftest.
from jira_adf import is_adf_dict, wrap_text_as_adf

# -----------------------------------------------------------------------------
# wrap_text_as_adf
# -----------------------------------------------------------------------------


class TestWrapTextAsAdf:
    """Plain-text → ADF wrapping behaviour."""

    def test_plain_text_single_line(self):
        result = wrap_text_as_adf("hello")
        assert result == {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "hello"}],
                },
            ],
        }

    def test_empty_string_returns_empty_paragraph(self):
        result = wrap_text_as_adf("")
        # Atlassian still treats an empty paragraph as a valid ADF doc.
        assert result == {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": []}],
        }

    def test_multiline_splits_into_paragraphs(self):
        result = wrap_text_as_adf("line one\nline two")
        assert result["type"] == "doc"
        assert result["version"] == 1
        assert len(result["content"]) == 2
        assert result["content"][0] == {
            "type": "paragraph",
            "content": [{"type": "text", "text": "line one"}],
        }
        assert result["content"][1] == {
            "type": "paragraph",
            "content": [{"type": "text", "text": "line two"}],
        }

    def test_blank_line_between_paragraphs_preserved(self):
        result = wrap_text_as_adf("first\n\nsecond")
        # Blank line → empty paragraph between two text paragraphs.
        assert len(result["content"]) == 3
        assert result["content"][0]["content"] == [{"type": "text", "text": "first"}]
        assert result["content"][1] == {"type": "paragraph", "content": []}
        assert result["content"][2]["content"] == [{"type": "text", "text": "second"}]

    def test_trailing_newline_emits_empty_final_paragraph(self):
        result = wrap_text_as_adf("trailing\n")
        # ``"trailing\n".split("\n")`` -> ``["trailing", ""]`` -> two
        # paragraphs (one with text, one empty).
        assert len(result["content"]) == 2
        assert result["content"][0]["content"][0]["text"] == "trailing"
        assert result["content"][1]["content"] == []

    def test_unicode_passthrough(self):
        result = wrap_text_as_adf("héllo 🌍")
        assert result["content"][0]["content"][0]["text"] == "héllo 🌍"

    def test_none_normalised_to_empty(self):
        # Defensive — callers should pass strings, but the helper coerces.
        result = wrap_text_as_adf(None)  # type: ignore[arg-type]
        assert result == {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": []}],
        }

    def test_non_string_coerced_via_str(self):
        # Passing e.g. a Path or int shouldn't raise — we'll wrap its
        # ``str()`` form.
        result = wrap_text_as_adf(42)  # type: ignore[arg-type]
        assert result["content"][0]["content"][0]["text"] == "42"

    def test_long_single_line_is_one_paragraph(self):
        text = "x" * 5000
        result = wrap_text_as_adf(text)
        assert len(result["content"]) == 1
        assert result["content"][0]["content"][0]["text"] == text

    @pytest.mark.parametrize(
        "text, expected_lines",
        [
            ("a\nb\nc", ["a", "b", "c"]),
            ("only", ["only"]),
            ("\nleading", ["", "leading"]),
        ],
    )
    def test_paragraph_count(self, text: str, expected_lines: list[str]):
        result = wrap_text_as_adf(text)
        actual = []
        for paragraph in result["content"]:
            if paragraph["content"]:
                actual.append(paragraph["content"][0]["text"])
            else:
                actual.append("")
        assert actual == expected_lines


# -----------------------------------------------------------------------------
# is_adf_dict
# -----------------------------------------------------------------------------


class TestIsAdfDict:
    """Structural test for "looks like an Atlassian ADF doc"."""

    def test_minimal_atlassian_sample_accepted(self):
        sample: dict[str, Any] = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "hi"}],
                },
            ],
        }
        assert is_adf_dict(sample) is True

    def test_round_trip_with_wrap_text_as_adf(self):
        # The output of wrap_text_as_adf must satisfy is_adf_dict so the
        # JiraClient passthrough check ("if it's already ADF, don't re-wrap")
        # behaves consistently.
        assert is_adf_dict(wrap_text_as_adf("hello")) is True
        assert is_adf_dict(wrap_text_as_adf("")) is True
        assert is_adf_dict(wrap_text_as_adf("a\nb")) is True

    def test_rich_sample_with_marks_accepted(self):
        # A more realistic ADF body with bold marks.
        sample = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "bold word",
                            "marks": [{"type": "strong"}],
                        }
                    ],
                }
            ],
        }
        assert is_adf_dict(sample) is True

    def test_empty_content_list_still_accepted(self):
        # Atlassian allows an "empty" doc — content can be ``[]``.  The
        # structural test only checks the outer envelope.
        assert is_adf_dict({"type": "doc", "version": 1, "content": []}) is True

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "plain string",
            42,
            3.14,
            True,
            [],
            ["doc", 1, []],  # list, not dict
            (),  # tuple
            object(),
        ],
    )
    def test_non_dict_rejected(self, value: object):
        assert is_adf_dict(value) is False

    @pytest.mark.parametrize(
        "value",
        [
            {},
            {"type": "paragraph"},  # wrong outer type
            {"type": "doc"},  # missing version + content
            {"type": "doc", "version": 1},  # missing content
            {"type": "doc", "content": []},  # missing version
            {"version": 1, "content": []},  # missing type
            {"type": "Doc", "version": 1, "content": []},  # case-sensitive
            {"type": "doc", "version": "1", "content": []},  # version not int
            {"type": "doc", "version": 1, "content": "oops"},  # content not list
            {"type": "doc", "version": 1, "content": {}},  # content not list
        ],
    )
    def test_malformed_dict_rejected(self, value: dict):
        assert is_adf_dict(value) is False

    def test_extra_keys_tolerated(self):
        # ADF v1 allows top-level extras (e.g. ``attrs``).  We don't reject
        # them — the structural shape we care about is still present.
        sample = {
            "type": "doc",
            "version": 1,
            "content": [],
            "attrs": {"foo": "bar"},
        }
        assert is_adf_dict(sample) is True

    def test_path_object_rejected(self):
        # Defence-in-depth — Path is dict-like in some Python contexts but
        # the isinstance check protects us.
        assert is_adf_dict(Path("/tmp")) is False


# -----------------------------------------------------------------------------
# Integration: wrap_text_as_adf output is a valid ADF doc
# -----------------------------------------------------------------------------


class TestWrapAndCheckRoundTrip:
    """End-to-end: wrap → is_adf_dict → True for representative inputs."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "single",
            "a\nb",
            "\n",
            "many\nlines\nhere",
            "with\n\nblank line",
            "trailing\n",
            "\nleading",
            "héllo",
        ],
    )
    def test_wrap_then_check(self, text: str):
        assert is_adf_dict(wrap_text_as_adf(text)) is True
