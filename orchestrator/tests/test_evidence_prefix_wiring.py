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
import evidence_gatherer
import pytest
import routes.pipelines as _pkg
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


# ---------------------------------------------------------------------------
# LIVE prompt path — _build_review_prompt (the reviewer's blocking concern)
# ---------------------------------------------------------------------------


class TestLiveReviewPromptWiring:
    """The seam is actually wired into the live reviewer-prompt assembler.

    Guards the #3523 S7 acceptance at RUNTIME, not just at the seam function:
    under `on` the live prompt carries the shared prefix; under off/log it is
    byte-identical to legacy; the tester/finding-verifier never reach this path.
    """

    @pytest.fixture
    def stub_pack(self, monkeypatch):
        """Stub the read-only git listing + gather so no real git is needed."""
        pack = build_pack(diff="D", files=[], symbols=[], environment={"python_version": "3.14.0"})
        monkeypatch.setattr(
            _pkg, "_list_changed_files_for_review", lambda repo_path, base_ref: ["x.py"]
        )
        monkeypatch.setattr(
            evidence_gatherer, "gather_evidence", lambda changed, repo, base_ref=None: pack
        )
        return pack

    def _build(self, reviewer_type="code"):
        return _pkg._build_review_prompt(
            phase="implement",
            pipeline_id="issue-3523",
            pipeline_mode="concurrent",
            reviewer_type=reviewer_type,
            repo_path="/tmp",
            base_branch="main",
            concurrent=True,
        )

    def test_off_and_log_byte_identical_to_legacy(self, monkeypatch, stub_pack):
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "off")
        off = self._build()
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "log")
        log = self._build()
        assert off == log
        # neither carries the shared prefix
        assert _SHARED_EVIDENCE_SYSTEM_PREFIX not in off

    def test_on_prepends_byte_identical_prefix(self, monkeypatch, stub_pack):
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "off")
        off = self._build()
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "on")
        on = self._build()
        prefix = build_shared_evidence_prefix(stub_pack)
        assert on.startswith(prefix)
        assert on.endswith(off)  # legacy body preserved verbatim at the tail
        assert _SHARED_EVIDENCE_SYSTEM_PREFIX in on

    def test_on_prefix_shared_across_sibling_lenses(self, monkeypatch, stub_pack):
        """Two different live reviewer prompts share the identical leading span."""
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "on")
        code = self._build(reviewer_type="code")
        security = self._build(reviewer_type="security")
        prefix = build_shared_evidence_prefix(stub_pack)
        assert code.startswith(prefix)
        assert security.startswith(prefix)
        assert code[: len(prefix)] == security[: len(prefix)]

    def test_log_mode_emits_record(self, monkeypatch, stub_pack):
        """log mode records the would-be prefix into the BRC log stream."""
        captured = {}

        class _Logger:
            def info(self, msg, **kw):
                captured["msg"] = msg
                captured["kw"] = kw

        monkeypatch.setattr(_pkg, "logger", _Logger())
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "log")
        self._build(reviewer_type="code")
        assert captured["kw"]["kind"] == "evidence_prefix"
        assert captured["kw"]["mode"] == "log"
        assert captured["kw"]["wave_roles"] == ["reviewer_code"]
        assert captured["kw"]["shared_prefix_bytes"] > 0

    def test_maybe_apply_off_is_noop_without_git(self, monkeypatch):
        """off mode returns the prompt unchanged without touching git."""
        monkeypatch.setenv("EGG_REVIEW_EVIDENCE_PREFIX", "off")

        def _boom(*a, **k):  # pragma: no cover - must not be called
            raise AssertionError("off mode must not gather evidence")

        monkeypatch.setattr(_pkg, "_list_changed_files_for_review", _boom)
        out = _pkg._maybe_apply_evidence_prefix(
            "BODY", reviewer_type="code", repo_path="/tmp", base_ref="origin/main"
        )
        assert out == "BODY"
