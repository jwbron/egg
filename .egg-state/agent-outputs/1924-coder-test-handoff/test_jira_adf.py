"""
Tests for ``gateway/jira_adf.py``.

Covers TASK-5-2 acceptance criteria:

- ``wrap_text_as_adf`` returns the minimal ADF document for plain text.
- Empty string is wrapped as an empty paragraph (matches Atlassian's
  shape for an empty comment).
- ``None`` and non-string inputs raise ``TypeError``.
- ``is_adf_dict`` returns True for a structurally valid envelope.
- ``is_adf_dict`` returns False for everything else: non-dicts, missing
  ``type``, wrong ``type``, non-int ``version``, ``version`` as bool,
  non-list ``content``.
"""

from __future__ import annotations

import jira_adf
import pytest


class TestWrapTextAsAdf:
    def test_plain_text_round_trip(self):
        doc = jira_adf.wrap_text_as_adf("hello world")
        assert doc == {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "hello world"}],
                }
            ],
        }

    def test_empty_string_returns_empty_paragraph(self):
        doc = jira_adf.wrap_text_as_adf("")
        assert doc == {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": []}],
        }

    def test_newlines_preserved(self):
        doc = jira_adf.wrap_text_as_adf("line1\nline2\nline3")
        assert doc["content"][0]["content"][0]["text"] == "line1\nline2\nline3"

    def test_unicode_passes_through(self):
        doc = jira_adf.wrap_text_as_adf("héllo 🌍")
        assert doc["content"][0]["content"][0]["text"] == "héllo 🌍"

    def test_returns_new_dict_each_call(self):
        a = jira_adf.wrap_text_as_adf("x")
        b = jira_adf.wrap_text_as_adf("x")
        assert a == b
        # Mutating ``a`` must not affect ``b``.
        a["content"][0]["content"][0]["text"] = "mutated"
        assert b["content"][0]["content"][0]["text"] == "x"

    def test_none_rejected(self):
        with pytest.raises(TypeError, match="None"):
            jira_adf.wrap_text_as_adf(None)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [0, 1.5, [], {}, b"bytes"])
    def test_non_string_rejected(self, bad):
        with pytest.raises(TypeError):
            jira_adf.wrap_text_as_adf(bad)  # type: ignore[arg-type]


class TestIsAdfDict:
    def test_valid_minimal_doc(self):
        assert jira_adf.is_adf_dict({"type": "doc", "version": 1, "content": []})

    def test_valid_doc_with_paragraph(self):
        assert jira_adf.is_adf_dict(jira_adf.wrap_text_as_adf("hello"))

    @pytest.mark.parametrize(
        "bad",
        [
            None,
            "string",
            42,
            1.5,
            [],
            (),
            b"bytes",
        ],
    )
    def test_non_dict_rejected(self, bad):
        assert not jira_adf.is_adf_dict(bad)

    def test_missing_type_rejected(self):
        assert not jira_adf.is_adf_dict({"version": 1, "content": []})

    def test_wrong_type_rejected(self):
        assert not jira_adf.is_adf_dict({"type": "paragraph", "version": 1, "content": []})

    def test_missing_version_rejected(self):
        assert not jira_adf.is_adf_dict({"type": "doc", "content": []})

    def test_string_version_rejected(self):
        assert not jira_adf.is_adf_dict({"type": "doc", "version": "1", "content": []})

    def test_bool_version_rejected(self):
        """Python booleans are a subclass of int — guard against truthy bools sneaking in."""
        assert not jira_adf.is_adf_dict({"type": "doc", "version": True, "content": []})

    def test_missing_content_rejected(self):
        assert not jira_adf.is_adf_dict({"type": "doc", "version": 1})

    def test_non_list_content_rejected(self):
        assert not jira_adf.is_adf_dict({"type": "doc", "version": 1, "content": "not a list"})
