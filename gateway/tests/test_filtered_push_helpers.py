"""Pure-Python helper tests for gateway/filtered_push.py (#1882).

These cover the internal helpers that don't need a real git repo — the
trailer-safe message composer and the parent translator.  The main
``execute_filtered_push`` end-to-end tests (which need a live git repo
via ``git init``) live in ``test_execute_filtered_push.py``; those are
skipped in the gateway-protected sandbox where ``git init`` is blocked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_gateway_path = Path(__file__).parent.parent
if str(_gateway_path) not in sys.path:
    sys.path.insert(0, str(_gateway_path))

from filtered_push import (  # type: ignore[import-not-found]
    _compose_filtered_message,
    _translate_parents,
)

# ---------------------------------------------------------------------------
# _compose_filtered_message — trailer preservation (NACK blocker #2)
# ---------------------------------------------------------------------------


class TestComposeFilteredMessage:
    """The auto-filter suffix must never glue into a trailer line.

    Git parses trailers from the *last paragraph*.  If we append
    `` [auto-filtered]`` to the last non-blank line, a message with a
    Signed-off-by / Co-Authored-By / DCO trailer gets its trailer line
    corrupted into ``Signed-off-by: alice <a@x> [auto-filtered]``, which
    breaks ``git interpret-trailers`` and GitHub's Co-Authored-By
    rendering.  The composer must emit the marker as its own paragraph.
    """

    def test_simple_one_line_message(self):
        result = _compose_filtered_message("feat: add widget", " [auto-filtered]")
        assert result == "feat: add widget\n\n[auto-filtered]\n"

    def test_multi_paragraph_message(self):
        msg = "feat: add widget\n\nLonger explanation of why.\n"
        result = _compose_filtered_message(msg, " [auto-filtered]")
        assert result == "feat: add widget\n\nLonger explanation of why.\n\n[auto-filtered]\n"

    def test_preserves_signed_off_by_trailer(self):
        """Signed-off-by must end up on its own paragraph, not glued."""
        msg = "feat: foo\n\nSigned-off-by: alice <a@x>\n"
        result = _compose_filtered_message(msg, " [auto-filtered]")
        # The trailer block remains its own paragraph and the marker is
        # a separate paragraph — two blank lines between them.
        assert "Signed-off-by: alice <a@x>\n\n[auto-filtered]" in result
        # The trailer is NOT glued.
        assert "Signed-off-by: alice <a@x> [auto-filtered]" not in result
        # The trailer still ends cleanly so ``git interpret-trailers``
        # can find it.
        assert "Signed-off-by: alice <a@x>" in result

    def test_preserves_co_authored_by_trailer(self):
        """Co-Authored-By (multi-line trailer block) survives."""
        msg = "feat: foo\n\nCo-authored-by: bob <b@x>\nCo-authored-by: carol <c@x>\n"
        result = _compose_filtered_message(msg, " [auto-filtered]")
        assert "Co-authored-by: bob <b@x>" in result
        assert "Co-authored-by: carol <c@x>" in result
        # Marker paragraph is appended after the trailer block.
        assert "Co-authored-by: carol <c@x>\n\n[auto-filtered]" in result
        # NOT glued.
        assert "carol <c@x> [auto-filtered]" not in result

    def test_message_with_trailing_whitespace(self):
        """Extra trailing newlines collapse; the composer still emits a
        single separator paragraph before the marker."""
        msg = "feat: foo\n\n\n\n"
        result = _compose_filtered_message(msg, " [auto-filtered]")
        assert result == "feat: foo\n\n[auto-filtered]\n"

    def test_empty_suffix_is_noop(self):
        """If the suffix is empty the message just gets a final
        newline — the marker is not appended."""
        msg = "feat: foo"
        result = _compose_filtered_message(msg, "")
        assert result == "feat: foo\n"

    def test_empty_message_only_emits_marker(self):
        result = _compose_filtered_message("", " [auto-filtered]")
        # An empty message with only the marker paragraph.
        assert result.endswith("[auto-filtered]\n")

    def test_suffix_is_stripped_of_outer_whitespace(self):
        """Suffix `` [auto-filtered]`` (with leading space) must be
        trimmed before becoming a paragraph — a paragraph cannot start
        with whitespace."""
        result = _compose_filtered_message("feat: foo", "    [auto-filtered]    ")
        # No indented whitespace before the marker.
        assert "\n[auto-filtered]\n" in result
        assert "\n    [auto-filtered]" not in result


# ---------------------------------------------------------------------------
# _translate_parents — multi-parent merge preservation (NACK blocker #1)
# ---------------------------------------------------------------------------


class TestTranslateParents:
    """Merge commits have 2+ parents; the rewriter must preserve all of
    them.  The old single-``-p`` code path silently dropped the 2nd+
    parents — reviewer_code flagged this as blocking."""

    def test_single_parent_already_matches_running_tip(self):
        """Chain unchanged — first parent matches ``new_parent``."""
        result = _translate_parents(
            orig_parents=["abc123"],
            parent_lookup={},
            new_parent="abc123",
        )
        assert result == ["abc123"]

    def test_single_parent_chain_shift(self):
        """First parent gets replaced with the new running tip."""
        result = _translate_parents(
            orig_parents=["original_parent"],
            parent_lookup={},
            new_parent="rewritten_parent",
        )
        assert result == ["rewritten_parent"]

    def test_merge_commit_two_parents_preserved(self):
        """Merge commit with two unrewritten parents keeps both."""
        result = _translate_parents(
            orig_parents=["main_tip", "feature_tip"],
            parent_lookup={},
            new_parent="main_tip",  # no chain shift on first parent
        )
        # Both parents kept, in order.
        assert result == ["main_tip", "feature_tip"]

    def test_merge_commit_first_parent_rewritten(self):
        """First parent shifted because earlier own-commit got
        rewritten; second parent passes through unchanged."""
        result = _translate_parents(
            orig_parents=["old_main", "feature_tip"],
            parent_lookup={},
            new_parent="new_main",
        )
        assert result == ["new_main", "feature_tip"]

    def test_merge_commit_second_parent_via_lookup(self):
        """2nd parent was rewritten earlier — it maps through
        parent_lookup instead of falling through unchanged."""
        result = _translate_parents(
            orig_parents=["main_tip", "feature_original"],
            parent_lookup={"feature_original": "feature_rewritten"},
            new_parent="main_tip",
        )
        assert result == ["main_tip", "feature_rewritten"]

    def test_three_parent_octopus_merge(self):
        """Octopus merges with 3+ parents: all preserved, each parent
        individually translated."""
        result = _translate_parents(
            orig_parents=["p1", "p2", "p3"],
            parent_lookup={"p2": "p2_new"},
            new_parent="p1",
        )
        # p1 unchanged (matches new_parent), p2 translated, p3 unchanged.
        assert result == ["p1", "p2_new", "p3"]

    def test_root_commit_no_parents(self):
        """A root commit (no parents) yields no ``-p`` flags."""
        result = _translate_parents(
            orig_parents=[],
            parent_lookup={},
            new_parent="some_tip",
        )
        assert result == []

    def test_empty_new_parent_falls_back_to_original_first(self):
        """If the running tip is empty (``None``/``""``) and the commit
        does have a first parent, we emit the original first parent so
        we never drop it silently."""
        result = _translate_parents(
            orig_parents=["existing_first"],
            parent_lookup={},
            new_parent=None,
        )
        assert result == ["existing_first"]

    def test_lookup_collision_with_matching_new_parent(self):
        """If the lookup maps a parent to itself (no-op), we still emit
        that parent — no silent deduplication."""
        result = _translate_parents(
            orig_parents=["a", "b"],
            parent_lookup={"b": "b"},  # identity mapping
            new_parent="a",
        )
        assert result == ["a", "b"]


# ---------------------------------------------------------------------------
# Importable and signature sanity
# ---------------------------------------------------------------------------


def test_commit_tree_accepts_list_signature():
    """``_commit_tree`` now accepts a list of parent SHAs; the old
    single-``str`` signature remains back-compat."""
    import inspect

    import filtered_push  # type: ignore[import-not-found]

    sig = inspect.signature(filtered_push._commit_tree)
    # The parameter's annotation must include ``list[str]`` (or just be
    # broader than a single ``str | None``) to lock in the fix.
    anno = sig.parameters["parent_shas"].annotation
    # The source annotation is ``list[str] | str | None`` — check the
    # stringified form rather than evaluating the generic.
    assert "list" in str(anno)


if __name__ == "__main__":  # pragma: no cover - manual run
    sys.exit(pytest.main([__file__, "-v"]))
