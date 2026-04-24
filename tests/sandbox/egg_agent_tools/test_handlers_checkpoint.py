"""Unit tests for egg_agent_tools.handlers.checkpoint (iter-2 #1917).

Covers the handler entry points ``checkpoint_list`` / ``checkpoint_show``
/ ``checkpoint_search`` in
``sandbox/egg_agent_tools/handlers/checkpoint.py``.  The shared
helpers (``collect_checkpoints`` / ``load_checkpoint`` /
``search_checkpoints``) live in ``shared/egg_contracts/checkpoint_cli.py``
— reviewer_code NACK #3 moved them there so the dependency direction
runs shared → sandbox-only.

Each handler imports its helper at call time, so ``patch(
"egg_contracts.checkpoint_cli.<name>")`` replaces the dispatch path
without the tests ever needing to hit the real git branch.  The
handler layer's responsibility is pagination + shape, so the tests
focus on:

- cursor encode / decode invariants
- limit coercion + caps
- pagination boundaries on list / search
- handler-level error translation
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import checkpoint  # noqa: E402
from egg_agent_tools.handlers.errors import HandlerError  # noqa: E402

# --------------------------------------------------------------------
# Cursor / limit helpers (handler-side)
# --------------------------------------------------------------------


class TestCursorEncoding:
    def test_roundtrip(self):
        for offset in (0, 1, 50, 10_000):
            assert checkpoint._decode_cursor(checkpoint._encode_cursor(offset)) == offset

    def test_none_cursor_returns_zero(self):
        assert checkpoint._decode_cursor(None) == 0

    def test_bad_cursor_raises(self):
        with pytest.raises(HandlerError):
            checkpoint._decode_cursor("$$not-base64$$")

    def test_negative_offset_rejected(self):
        bad = base64.urlsafe_b64encode(b'{"offset": -1}').decode().rstrip("=")
        with pytest.raises(HandlerError):
            checkpoint._decode_cursor(bad)

    def test_non_string_rejected(self):
        with pytest.raises(HandlerError):
            checkpoint._decode_cursor(123)


class TestLimitCoercion:
    def test_default_when_none(self):
        assert checkpoint._coerce_limit(None, default=100) == 100

    def test_positive_int_accepted(self):
        assert checkpoint._coerce_limit(25, default=100) == 25

    def test_zero_rejected(self):
        with pytest.raises(HandlerError):
            checkpoint._coerce_limit(0, default=100)

    def test_negative_rejected(self):
        with pytest.raises(HandlerError):
            checkpoint._coerce_limit(-1, default=100)

    def test_over_max_rejected(self):
        with pytest.raises(HandlerError):
            checkpoint._coerce_limit(1000, default=100)

    def test_non_integer_rejected(self):
        with pytest.raises(HandlerError):
            checkpoint._coerce_limit("many", default=100)


# --------------------------------------------------------------------
# Handler entry points
# --------------------------------------------------------------------


def _items(count: int):
    return [{"id": f"ckpt-{i:04x}", "order": i} for i in range(count)]


def _collected(items, *, ref="abc123", checkpoint_repo=None):
    return {
        "checkpoints": items,
        "composite_role": None,
        "ref": ref,
        "checkpoint_repo": checkpoint_repo,
    }


class TestCheckpointList:
    def _patch_collect(self, items, **kwargs):
        return patch(
            "egg_contracts.checkpoint_cli.collect_checkpoints",
            return_value=_collected(items, **kwargs),
        )

    def test_empty_page(self):
        with self._patch_collect([]):
            resp = checkpoint.checkpoint_list({})
        assert resp["ok"] is True
        assert resp["items"] == []
        assert resp["total_available"] == 0
        assert resp["next_cursor"] is None

    def test_single_page_under_default_limit(self):
        with self._patch_collect(_items(50)):
            resp = checkpoint.checkpoint_list({})
        assert len(resp["items"]) == 50
        assert resp["total_available"] == 50
        assert resp["next_cursor"] is None

    def test_exact_limit_page_no_next_cursor(self):
        with self._patch_collect(_items(100)):
            resp = checkpoint.checkpoint_list({"limit": 100})
        assert len(resp["items"]) == 100
        assert resp["next_cursor"] is None

    def test_pagination_beyond_limit(self):
        items = _items(250)
        with self._patch_collect(items):
            resp = checkpoint.checkpoint_list({"limit": 100})
        assert len(resp["items"]) == 100
        assert resp["next_cursor"] is not None

        with self._patch_collect(items):
            resp2 = checkpoint.checkpoint_list({"limit": 100, "cursor": resp["next_cursor"]})
        assert len(resp2["items"]) == 100
        assert resp2["next_cursor"] is not None

        with self._patch_collect(items):
            resp3 = checkpoint.checkpoint_list({"limit": 100, "cursor": resp2["next_cursor"]})
        assert len(resp3["items"]) == 50
        assert resp3["next_cursor"] is None

    def test_bad_cursor_rejected(self):
        with self._patch_collect(_items(10)):
            with pytest.raises(HandlerError):
                checkpoint.checkpoint_list({"cursor": "$$not-base64$$"})

    def test_limit_cap_enforced(self):
        with self._patch_collect(_items(10)):
            with pytest.raises(HandlerError):
                checkpoint.checkpoint_list({"limit": 10_000})

    def test_zero_limit_rejected(self):
        with self._patch_collect(_items(10)):
            with pytest.raises(HandlerError):
                checkpoint.checkpoint_list({"limit": 0})

    def test_ref_and_checkpoint_repo_surface_in_response(self):
        with self._patch_collect(
            [],
            ref="refs/heads/egg/checkpoints/v2",
            checkpoint_repo="owner/repo",
        ):
            resp = checkpoint.checkpoint_list({})
        assert resp["ref"] == "refs/heads/egg/checkpoints/v2"
        assert resp["checkpoint_repo"] == "owner/repo"


class TestCheckpointShow:
    def test_missing_identifier(self):
        with pytest.raises(HandlerError):
            checkpoint.checkpoint_show({})

    def test_non_string_identifier_rejected(self):
        with pytest.raises(HandlerError):
            checkpoint.checkpoint_show({"identifier": 42})

    def test_unknown_identifier_raises(self):
        with patch(
            "egg_contracts.checkpoint_cli.load_checkpoint",
            return_value=None,
        ):
            with pytest.raises(HandlerError) as exc:
                checkpoint.checkpoint_show({"identifier": "ckpt-missing"})
        assert "No checkpoint found" in str(exc.value)

    def test_happy_path(self):
        payload = {"id": "ckpt-0001", "session": {"agent_role": "coder"}}
        with patch(
            "egg_contracts.checkpoint_cli.load_checkpoint",
            return_value=payload,
        ):
            resp = checkpoint.checkpoint_show({"identifier": "ckpt-0001"})
        assert resp["ok"] is True
        assert resp["checkpoint"] == payload


class TestCheckpointSearch:
    def _patch_search(self, matches, **kwargs):
        return patch(
            "egg_contracts.checkpoint_cli.search_checkpoints",
            return_value={
                "matches": matches,
                "composite_role": None,
                "ref": kwargs.get("ref", "abc"),
                "checkpoint_repo": kwargs.get("checkpoint_repo"),
                "query": kwargs.get("query", "q"),
            },
        )

    def test_requires_text_or_query(self):
        with pytest.raises(HandlerError):
            checkpoint.checkpoint_search({})

    def test_accepts_query_alias(self):
        """When the caller uses the ``query`` alias instead of
        ``text``, the handler still forwards the substring."""
        with self._patch_search([], query="hi"):
            resp = checkpoint.checkpoint_search({"query": "hi"})
        assert resp["query"] == "hi"

    def test_empty_matches(self):
        with self._patch_search([]):
            resp = checkpoint.checkpoint_search({"text": "none"})
        assert resp["items"] == []
        assert resp["next_cursor"] is None

    def test_pagination(self):
        matches = [{"summary": {"id": f"ckpt-{i:04x}"}, "snippets": ["x"]} for i in range(250)]
        with self._patch_search(matches):
            resp = checkpoint.checkpoint_search({"text": "x", "limit": 100})
        assert len(resp["items"]) == 100
        assert resp["next_cursor"] is not None

        with self._patch_search(matches):
            resp2 = checkpoint.checkpoint_search(
                {"text": "x", "limit": 100, "cursor": resp["next_cursor"]}
            )
        assert len(resp2["items"]) == 100

        with self._patch_search(matches):
            resp3 = checkpoint.checkpoint_search(
                {"text": "x", "limit": 100, "cursor": resp2["next_cursor"]}
            )
        assert len(resp3["items"]) == 50
        assert resp3["next_cursor"] is None

    def test_bad_cursor_rejected(self):
        with self._patch_search([]):
            with pytest.raises(HandlerError):
                checkpoint.checkpoint_search({"text": "hi", "cursor": "$$bad$$"})

    def test_limit_cap_enforced(self):
        with self._patch_search([]):
            with pytest.raises(HandlerError):
                checkpoint.checkpoint_search({"text": "hi", "limit": 10_000})


class TestCursorRoundtripsJsonEncoded:
    """The cursor format is implementation-defined but must be opaque
    and round-trip through base64 cleanly.  Lock the invariant:
    whatever the handler emits can be fed straight back in."""

    def test_list_cursor_fed_back_in_yields_next_page(self):
        items = _items(60)
        with patch(
            "egg_contracts.checkpoint_cli.collect_checkpoints",
            return_value=_collected(items),
        ):
            first = checkpoint.checkpoint_list({"limit": 25})
            assert first["next_cursor"] is not None
            padding = "=" * (-len(first["next_cursor"]) % 4)
            raw = base64.urlsafe_b64decode(first["next_cursor"] + padding)
            assert json.loads(raw)["offset"] == 25
            second = checkpoint.checkpoint_list({"limit": 25, "cursor": first["next_cursor"]})
        # 26th element (0-indexed 25) — id uses lowercase hex width 4.
        assert second["items"][0]["id"] == "ckpt-0019"
