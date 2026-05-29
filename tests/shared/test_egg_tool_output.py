"""Unit tests for the shared tool-output cap helper (issue #2805).

The Agent SDK message reader crashes the agent when a tool result exceeds
its 1 MB JSON buffer (#2804). These helpers bound the payload before it
gets there. The invariants under test:

* small payloads pass through untouched,
* oversized payloads are replaced with a structured marker that itself
  fits under the cap (no point capping if the marker overflows),
* the marker carries a head preview + a "how to narrow" hint,
* the file-spill path writes the full output and returns a small preview.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))

from egg_tool_output import (  # noqa: E402
    SPILL_KEY,
    TRUNCATION_KEY,
    cap_bytes_from_env,
    cap_result_dict,
    cap_text,
    spill_to_file,
)


def _utf8(s: str) -> int:
    return len(s.encode("utf-8"))


class TestCapText:
    def test_small_text_passes_through(self):
        assert cap_text("hello", cap_bytes=1000) == "hello"

    def test_oversized_text_truncated_to_marker(self):
        big = "x" * 5000
        out = cap_text(big, tool="t", cap_bytes=1000)
        assert out != big
        marker = json.loads(out)
        assert marker[TRUNCATION_KEY] is True
        assert marker["tool"] == "t"
        assert marker["original_bytes"] == 5000

    def test_marker_fits_under_cap(self):
        # Even a pathological payload of escape-heavy chars must not produce
        # a marker that itself exceeds the cap.
        big = '"\n' * 50000
        out = cap_text(big, cap_bytes=2000)
        assert _utf8(out) <= 2000

    def test_preview_is_head_of_original(self):
        big = "HEAD-MARKER" + "z" * 5000
        marker = json.loads(cap_text(big, cap_bytes=1000))
        assert marker["preview"].startswith("HEAD-MARKER")

    def test_custom_hint_in_note(self):
        marker = json.loads(cap_text("y" * 5000, cap_bytes=2000, narrow_hint="use frobnicate"))
        assert "use frobnicate" in marker["note"]

    def test_generic_hint_when_unset(self):
        marker = json.loads(cap_text("y" * 5000, cap_bytes=2000))
        assert "narrow the call" in marker["note"]


class TestCapResultDict:
    def test_small_dict_passes_through(self):
        d = {"ok": True, "n": 1}
        assert cap_result_dict(d, cap_bytes=1000) is d

    def test_oversized_dict_becomes_marker(self):
        d = {"rows": ["x" * 100 for _ in range(1000)]}
        out = cap_result_dict(d, tool="list_tasks", cap_bytes=1000)
        assert out[TRUNCATION_KEY] is True
        assert out["tool"] == "list_tasks"
        assert _utf8(json.dumps(out)) <= 1000

    def test_non_serializable_falls_back_to_str(self):
        class Weird:
            def __repr__(self) -> str:
                return "W" * 5000

        out = cap_result_dict({"x": Weird()}, cap_bytes=1000)
        # default=str makes it serializable, so it still caps cleanly.
        assert out[TRUNCATION_KEY] is True


class TestSpillToFile:
    def test_small_payload_returns_none(self, tmp_path):
        assert spill_to_file("small", tool="t", cap_bytes=1000) is None

    def test_oversized_payload_spilled(self, tmp_path):
        big = "\n".join(f"line-{i}" for i in range(5000))
        desc = spill_to_file(big, tool="checkpoint_show", cap_bytes=1000, spill_dir=str(tmp_path))
        assert desc is not None
        assert desc[SPILL_KEY] is True
        assert desc["total_bytes"] == _utf8(big)
        path = Path(desc["output_path"])
        assert path.exists()
        assert path.read_text(encoding="utf-8") == big
        # Preview is bounded and holds the head lines.
        assert "line-0" in desc["preview"]
        assert _utf8(json.dumps(desc)) <= 1000 + 4096  # descriptor stays small

    def test_unwritable_dir_returns_none(self):
        # A bogus spill dir must not raise — caller falls back to truncation.
        big = "x" * 5000
        assert (
            spill_to_file(big, tool="t", cap_bytes=1000, spill_dir="/nonexistent/egg/dir") is None
        )


class TestCapFromEnv:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("EGG_TOOL_OUTPUT_CAP_BYTES", raising=False)
        assert cap_bytes_from_env(default=123) == 123

    def test_override(self, monkeypatch):
        monkeypatch.setenv("EGG_TOOL_OUTPUT_CAP_BYTES", "4096")
        assert cap_bytes_from_env() == 4096

    def test_bad_value_falls_back(self, monkeypatch):
        monkeypatch.setenv("EGG_TOOL_OUTPUT_CAP_BYTES", "not-a-number")
        assert cap_bytes_from_env(default=777) == 777

    def test_nonpositive_falls_back(self, monkeypatch):
        monkeypatch.setenv("EGG_TOOL_OUTPUT_CAP_BYTES", "0")
        assert cap_bytes_from_env(default=777) == 777
