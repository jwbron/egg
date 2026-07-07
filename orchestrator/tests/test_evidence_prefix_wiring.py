"""Unit tests for the shared-evidence prefix wiring (#3523 §5, S7 / task-7-2).

Covers the task-7-2 acceptance:

- with the flag ``off`` / ``log``, reviewer prompt assembly is UNCHANGED
  (byte-identical to the legacy lens instruction);
- with the flag ``on``, sibling same-model reviewers share a BYTE-IDENTICAL
  prefix, with the per-lens instruction only at the tail;
- the tester and finding-verifier stay COLD-START (never get the prefix);
- ``log`` mode records the measured wave cache-hit rate + per-wave cost.
"""

from __future__ import annotations

import consensus_wrapper
import pytest
from consensus_wrapper import (
    aggregate_wave_cache_stats,
    evidence_prefix_log_record,
)
from evidence_gatherer import build_pack
from routes.pipelines._criteria import (
    _SHARED_EVIDENCE_SYSTEM_PREFIX,
    apply_shared_evidence_prefix,
    build_shared_evidence_prefix,
)


def _pack():
    return build_pack(
        diff="diff --git a/x b/x\n@@\n+changed\n",
        files=[],
        symbols=[],
        environment={"python_version": "3.12.0"},
    )


# ---------------------------------------------------------------------------
# _criteria.py — assembly seam
# ---------------------------------------------------------------------------


class TestApplySharedEvidencePrefix:
    LENS = "**CODE REVIEW** lens instruction tail."

    def test_off_leaves_lens_unchanged(self, monkeypatch):
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "off")
        out = apply_shared_evidence_prefix(
            self.LENS, reviewer_role="reviewer_code", evidence_pack=_pack()
        )
        assert out == self.LENS

    def test_log_leaves_lens_unchanged(self, monkeypatch):
        """log mode measures cost in the wrapper; it does NOT change assembly."""
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "log")
        out = apply_shared_evidence_prefix(
            self.LENS, reviewer_role="reviewer_code", evidence_pack=_pack()
        )
        assert out == self.LENS

    def test_unknown_flag_off(self, monkeypatch):
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "banana")
        out = apply_shared_evidence_prefix(
            self.LENS, reviewer_role="reviewer_code", evidence_pack=_pack()
        )
        assert out == self.LENS

    def test_on_prepends_prefix(self, monkeypatch):
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "on")
        pack = _pack()
        out = apply_shared_evidence_prefix(
            self.LENS, reviewer_role="reviewer_code", evidence_pack=pack
        )
        assert out.startswith(build_shared_evidence_prefix(pack))
        assert out.endswith(self.LENS)
        assert _SHARED_EVIDENCE_SYSTEM_PREFIX in out

    def test_on_none_pack_unchanged(self, monkeypatch):
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "on")
        out = apply_shared_evidence_prefix(
            self.LENS, reviewer_role="reviewer_code", evidence_pack=None
        )
        assert out == self.LENS

    def test_on_byte_identical_prefix_across_wave(self, monkeypatch):
        """Two different lenses in the wave carry the SAME leading prefix bytes."""
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "on")
        pack = _pack()
        code = apply_shared_evidence_prefix(
            "code lens tail", reviewer_role="reviewer_code", evidence_pack=pack
        )
        security = apply_shared_evidence_prefix(
            "security lens tail", reviewer_role="reviewer_security", evidence_pack=pack
        )
        prefix = build_shared_evidence_prefix(pack)
        assert code.startswith(prefix)
        assert security.startswith(prefix)
        # The shared span is genuinely identical (the cache-warming invariant).
        assert code[: len(prefix)] == security[: len(prefix)]

    def test_tester_stays_cold_start_even_on(self, monkeypatch):
        """Independence guardrail: the tester never inherits the shared prefix."""
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "on")
        out = apply_shared_evidence_prefix(self.LENS, reviewer_role="tester", evidence_pack=_pack())
        assert out == self.LENS

    def test_finding_verifier_stays_cold_start_even_on(self, monkeypatch):
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "on")
        out = apply_shared_evidence_prefix(
            self.LENS, reviewer_role="finding_verifier", evidence_pack=_pack()
        )
        assert out == self.LENS

    def test_build_prefix_is_pack_only(self, monkeypatch):
        """The prefix depends only on the pack, not the lens (pure)."""
        pack = _pack()
        assert build_shared_evidence_prefix(pack) == build_shared_evidence_prefix(pack)
        assert build_shared_evidence_prefix(None) == ""


# ---------------------------------------------------------------------------
# consensus_wrapper.py — log-mode measurement
# ---------------------------------------------------------------------------


class TestAggregateWaveCacheStats:
    def test_empty_wave(self):
        stats = aggregate_wave_cache_stats([])
        assert stats["sessions"] == 0
        assert stats["cache_hit_rate_pct"] is None
        assert stats["per_wave_cost"] is None

    def test_rolls_up_session_records(self):
        records = [
            {"session": {"prompt_tokens": 1000, "cached_tokens": 800, "cost": 0.10}},
            {"session": {"prompt_tokens": 1000, "cached_tokens": 900, "cost": 0.05}},
        ]
        stats = aggregate_wave_cache_stats(records)
        assert stats["sessions"] == 2
        assert stats["prompt_tokens"] == 2000
        assert stats["cached_tokens"] == 1700
        assert stats["cache_hit_rate_pct"] == 85.0
        assert stats["per_wave_cost"] == pytest.approx(0.15)
        assert stats["cost_known_sessions"] == 2

    def test_missing_cost_is_none_not_zero(self):
        """Absent cost reads as None (not captured), never silently 0."""
        records = [{"session": {"prompt_tokens": 100, "cached_tokens": 10}}]
        stats = aggregate_wave_cache_stats(records)
        assert stats["per_wave_cost"] is None
        assert stats["cache_hit_rate_pct"] == 10.0

    def test_reads_top_level_fields(self):
        """Records without a nested session dict read from the top level."""
        stats = aggregate_wave_cache_stats(
            [{"prompt_tokens": 200, "cached_tokens": 100, "cost": 0.2}]
        )
        assert stats["prompt_tokens"] == 200
        assert stats["cache_hit_rate_pct"] == 50.0
        assert stats["per_wave_cost"] == 0.2

    def test_hit_rate_clamped(self):
        stats = aggregate_wave_cache_stats([{"prompt_tokens": 100, "cached_tokens": 200}])
        assert stats["cache_hit_rate_pct"] == 100.0


class TestEvidencePrefixLogRecord:
    def test_record_shape(self, monkeypatch):
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "log")
        rec = evidence_prefix_log_record(
            wave_roles=["reviewer_security", "reviewer_code"],
            shared_prefix_bytes=4096,
            session_records=[
                {"session": {"prompt_tokens": 1000, "cached_tokens": 900, "cost": 0.02}},
            ],
        )
        assert rec["kind"] == "evidence_prefix"
        assert rec["mode"] == "log"
        # wave roles are sorted for a deterministic artifact.
        assert rec["wave_roles"] == ["reviewer_code", "reviewer_security"]
        assert rec["shared_prefix_bytes"] == 4096
        assert rec["cache_stats"]["cache_hit_rate_pct"] == 90.0

    def test_mode_injectable(self):
        rec = evidence_prefix_log_record(mode="on")
        assert rec["mode"] == "on"
        assert rec["wave_roles"] == []
        assert rec["cache_stats"]["sessions"] == 0

    def test_mode_defaults_to_live_flag(self, monkeypatch):
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "on")
        rec = evidence_prefix_log_record()
        assert rec["mode"] == "on"


def test_wrapper_reexports_flag_resolver():
    """consensus_wrapper imports the S7 flag resolver (no second switch)."""
    assert consensus_wrapper.evidence_prefix_mode() in {"off", "log", "on"}
