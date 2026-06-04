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
import os
import sys
import time
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

    def test_pathological_tiny_cap_drops_preview(self):
        # A cap smaller than the fixed marker scaffolding can't hold a preview;
        # the marker must stay minimal (sub-KB) rather than carry a preview.
        marker = json.loads(cap_text("x" * 5000, cap_bytes=50))
        assert marker[TRUNCATION_KEY] is True
        assert "preview" not in marker
        # Still tiny in absolute terms — never a threat to the 1 MB buffer.
        assert _utf8(json.dumps(marker)) < 1024


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

    def test_marker_fits_under_cap_at_caller_indent(self):
        # The orchestrator re-serializes the marker with indent=2; measuring
        # with the same indent guarantees the on-wire size honors the cap.
        d = {"rows": ["x" * 100 for _ in range(2000)]}
        out = cap_result_dict(d, tool="list_tasks", cap_bytes=4000, indent=2)
        assert out[TRUNCATION_KEY] is True
        assert _utf8(json.dumps(out, indent=2)) <= 4000


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

    def test_single_huge_line_preview_bounded(self, tmp_path):
        # A compact (one physical line) payload must still yield a small,
        # well-formed descriptor — the preview can't blow past its budget.
        big = "z" * (300 * 1024)
        desc = spill_to_file(big, tool="checkpoint_show", cap_bytes=1000, spill_dir=str(tmp_path))
        assert desc is not None
        assert _utf8(desc["preview"]) <= 4 * 1024
        # output_path survives — it's the field that makes spill useful.
        assert Path(desc["output_path"]).exists()
        assert _utf8(json.dumps(desc)) <= 1000 + 4096

    def test_preview_budget_scales_with_small_cap(self, tmp_path):
        # Under a cap smaller than the fixed 4 KB preview budget, the preview
        # must shrink to the cap rather than staying 4 KB (which would make the
        # descriptor dwarf the cap and risk the outer cap_text dropping
        # output_path). The full content is still preserved on disk.
        big = "z" * (300 * 1024)
        desc = spill_to_file(big, tool="checkpoint_show", cap_bytes=2000, spill_dir=str(tmp_path))
        assert desc is not None
        assert _utf8(desc["preview"]) <= 2000
        assert Path(desc["output_path"]).exists()

    def test_stale_spills_pruned(self, tmp_path, monkeypatch):
        # An old spill file is best-effort removed when a new one is written.
        stale = tmp_path / "egg-tool-out-old-deadbeef.txt"
        stale.write_text("old", encoding="utf-8")
        old_time = time.time() - (2 * 60 * 60)
        os.utime(stale, (old_time, old_time))

        big = "\n".join(f"line-{i}" for i in range(5000))
        desc = spill_to_file(big, tool="checkpoint_show", cap_bytes=1000, spill_dir=str(tmp_path))
        assert desc is not None
        assert not stale.exists()
        # The fresh spill is kept.
        assert Path(desc["output_path"]).exists()


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

    def test_bad_value_warns(self, monkeypatch, caplog):
        # Operator-set config that we can't honor must not vanish silently
        # (#2805 blocking item): warn so the operator knows it was dropped.
        monkeypatch.setenv("EGG_TOOL_OUTPUT_CAP_BYTES", "banana")
        with caplog.at_level("WARNING", logger="egg_tool_output"):
            assert cap_bytes_from_env(default=777) == 777
        assert any("EGG_TOOL_OUTPUT_CAP_BYTES" in r.message for r in caplog.records)

    def test_nonpositive_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("EGG_TOOL_OUTPUT_CAP_BYTES", "-5")
        with caplog.at_level("WARNING", logger="egg_tool_output"):
            assert cap_bytes_from_env(default=777) == 777
        assert any("EGG_TOOL_OUTPUT_CAP_BYTES" in r.message for r in caplog.records)
