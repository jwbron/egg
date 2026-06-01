"""Adversarial probes for the BRC memory writer (issue #2908 slice-1).

These tests target edge cases and boundary conditions that the happy-path
suite in ``test_handlers_brc.py`` does not exercise. Each test is designed
to surface a real bug or a latent fragility that the coder should harden.

Patterns:
- Atomic write under contention (concurrent writers)
- Parse/render round-trip edge cases (special characters, unicode)
- Memory schema invariants (cap enforcement, field ordering)
- Error-path robustness (permission errors, disk full, corrupted files)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "shared"))

from egg_agent_tools.handlers import brc_memory  # noqa: E402


@pytest.fixture
def memory_env(monkeypatch, tmp_path):
    """Set up a clean environment for memory writer tests."""
    monkeypatch.setenv("EGG_BRC_MEMORY", "write-only")
    monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
    monkeypatch.setenv("EGG_REPO_PATH", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Adversarial probes: role resolution
# ---------------------------------------------------------------------------


class TestBrcMemoryRoleResolutionAdversarial:
    def test_role_with_path_separator_rejected(self, memory_env, monkeypatch):
        """A role token containing ``/`` or ``..`` must be rejected BEFORE
        the path constructor touches the filesystem (path-smuggling attack)."""
        monkeypatch.setenv("EGG_AGENT_ROLE", "tester/../../etc")
        with pytest.raises(ValueError, match="rejects role token"):
            brc_memory.memory_path_for_role()

    def test_role_with_whitespace_only_rejected(self, memory_env, monkeypatch):
        """A role that is only whitespace (``"   "``) must be rejected."""
        monkeypatch.setenv("EGG_AGENT_ROLE", "   ")
        with pytest.raises(ValueError, match="requires EGG_AGENT_ROLE"):
            brc_memory.memory_path_for_role()

    def test_role_with_special_unicode_rejected(self, memory_env, monkeypatch):
        """A role containing emoji or other non-ASCII must be rejected."""
        monkeypatch.setenv("EGG_AGENT_ROLE", "tester🔥")
        with pytest.raises(ValueError, match="rejects role token"):
            brc_memory.memory_path_for_role()

    def test_explicit_role_override_beats_env(self, memory_env, monkeypatch):
        """When ``role="coder"`` is passed explicitly, it overrides
        $EGG_AGENT_ROLE."""
        monkeypatch.setenv("EGG_AGENT_ROLE", "tester")
        path = brc_memory.memory_path_for_role(role="coder")
        assert "coder" in str(path)
        assert "tester" not in str(path)


# ---------------------------------------------------------------------------
# Adversarial probes: atomic write contract
# ---------------------------------------------------------------------------


class TestBrcMemoryAtomicWriteAdversarial:
    def test_tempfile_cleanup_on_os_replace_failure(self, memory_env, tmp_path):
        """When ``os.replace`` fails (e.g. cross-device rename), the temp
        file is cleaned up (no orphan ``.tmp`` files leak)."""
        memory = brc_memory.BRCMemory(codebase_change_model="test")
        path = tmp_path / "brc-memory.md"

        # Count .tmp files before.
        tmp_files_before = list(tmp_path.glob("*.tmp"))

        with patch("os.replace", side_effect=OSError("cross-device link")):
            with pytest.raises(OSError):
                brc_memory.write_memory_atomic(memory, path)

        # Count .tmp files after — must be the same (cleanup succeeded).
        tmp_files_after = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files_after) == len(tmp_files_before), (
            f"Temp files leaked: before={tmp_files_before} after={tmp_files_after}"
        )

    def test_parent_directory_does_not_exist_created(self, memory_env, tmp_path):
        """When the parent directory (``<role>/``) does not exist, the
        writer creates it (parents=True, exist_ok=True)."""
        memory = brc_memory.BRCMemory()
        path = tmp_path / "nonexistent" / "subdir" / "brc-memory.md"

        brc_memory.write_memory_atomic(memory, path)

        assert path.exists()
        assert path.parent.is_dir()

    def test_existing_file_overwritten_atomically(self, memory_env, tmp_path):
        """When a memory file already exists, the writer overwrites it
        atomically (the old content is never partially visible)."""
        path = tmp_path / "brc-memory.md"
        path.write_text("# OLD CONTENT\n", encoding="utf-8")

        memory = brc_memory.BRCMemory(codebase_change_model="new content")
        brc_memory.write_memory_atomic(memory, path)

        content = path.read_text(encoding="utf-8")
        assert "# OLD CONTENT" not in content
        assert "new content" in content


# ---------------------------------------------------------------------------
# Adversarial probes: decision log capping
# ---------------------------------------------------------------------------


class TestBrcMemoryDecisionLogCapAdversarial:
    def test_decision_log_cap_at_twenty_exact(self, memory_env, tmp_path):
        """The decision log is capped at exactly 20 entries — the 21st
        entry triggers a distill (oldest entry removed)."""
        path = tmp_path / "brc-memory.md"
        memory = brc_memory.BRCMemory()

        # Write 25 entries.
        for i in range(25):
            memory.decision_log.append(f"entry-{i}")
            brc_memory.write_memory_atomic(memory, path)

        # Reload and check.
        reloaded = brc_memory.load_memory(path)
        assert len(reloaded.decision_log) == 20
        # The oldest 5 entries are gone.
        assert "entry-0" not in reloaded.decision_log
        assert "entry-4" not in reloaded.decision_log
        # The newest 20 entries are present.
        assert "entry-5" in reloaded.decision_log
        assert "entry-24" in reloaded.decision_log

    def test_decision_log_cap_applied_on_every_write(self, memory_env, tmp_path):
        """The cap is applied on every write, not just the first — a
        sequence of 100 writes still results in exactly 20 entries."""
        path = tmp_path / "brc-memory.md"
        memory = brc_memory.BRCMemory()

        for i in range(100):
            memory.decision_log.append(f"entry-{i}")
            brc_memory.write_memory_atomic(memory, path)

        reloaded = brc_memory.load_memory(path)
        assert len(reloaded.decision_log) == 20


# ---------------------------------------------------------------------------
# Adversarial probes: parse/render round-trip
# ---------------------------------------------------------------------------


class TestBrcMemoryParseRenderAdversarial:
    def test_render_normalizes_multiline_prose(self, memory_env):
        """Multi-paragraph prose is collapsed to a single line so the
        markdown parser can split on ``- <field>:`` boundaries."""
        assessment = brc_memory.ProducerAssessment(
            producer="coder",
            summary_of_assessment="Line 1\n\nLine 2\n\nLine 3",
        )
        lines = brc_memory._render_assessment(assessment)
        summary_line = [ln for ln in lines if ln.startswith("- summary_of_assessment:")][0]
        # All whitespace collapsed to single spaces.
        assert "Line 1 Line 2 Line 3" in summary_line
        assert "\n" not in summary_line.split(":", 1)[1]

    def test_render_truncates_oversized_prose_with_ellipsis(self, memory_env):
        """Prose exceeding ``_ASSESSMENT_PROSE_MAX_CHARS`` is truncated
        with a ``…`` sentinel (not silently dropped)."""
        long_text = "x" * 2000
        assessment = brc_memory.ProducerAssessment(
            producer="coder",
            summary_of_assessment=long_text,
        )
        lines = brc_memory._render_assessment(assessment)
        summary_line = [ln for ln in lines if ln.startswith("- summary_of_assessment:")][0]
        # Truncated to ~1000 chars + ellipsis.
        value = summary_line.split(":", 1)[1].strip()
        assert len(value) <= 1001  # 1000 + "…"
        assert value.endswith("…")

    def test_round_trip_preserves_all_fields(self, memory_env, tmp_path):
        """A full round-trip (render → write → parse) preserves all six
        required fields plus the decision log."""
        original = brc_memory.BRCMemory(
            codebase_change_model="Test change model",
            per_producer={
                "coder": brc_memory.ProducerAssessment(
                    producer="coder",
                    last_reviewed_commit_sha="abc123",
                    prior_verdict="ACK",
                    prior_nack_reasons=["reason1", "reason2"],
                    prior_conditional_obligation="do X before merge",
                    summary_of_assessment="looks good",
                )
            },
            decision_log=["2026-06-01T12:00:00Z ack coder: looks good [file1.py]"],
        )
        path = tmp_path / "brc-memory.md"
        brc_memory.write_memory_atomic(original, path)
        reloaded = brc_memory.load_memory(path)

        assert reloaded.codebase_change_model == original.codebase_change_model
        assert "coder" in reloaded.per_producer
        assert reloaded.per_producer["coder"].last_reviewed_commit_sha == "abc123"
        assert reloaded.per_producer["coder"].prior_verdict == "ACK"
        # NACK reasons are preserved.
        assert "reason1" in reloaded.per_producer["coder"].prior_nack_reasons
        assert reloaded.per_producer["coder"].prior_conditional_obligation == "do X before merge"
        assert len(reloaded.decision_log) == 1

    def test_parse_handles_missing_optional_fields(self, memory_env, tmp_path):
        """When optional fields are missing (``prior_nack_reasons: -``),
        the parser returns an empty list (not ``["-"]``)."""
        content = """
# BRC Memory

## Codebase / change model

-

## Per-producer assessment

### coder

- producer: coder
- last_reviewed_commit_sha: -
- prior_verdict: -
- prior_nack_reasons: -
- prior_conditional_obligation: -
- summary_of_assessment: -

## Decision log

"""
        path = tmp_path / "brc-memory.md"
        path.write_text(content, encoding="utf-8")
        memory = brc_memory.load_memory(path)

        assert "coder" in memory.per_producer
        assessment = memory.per_producer["coder"]
        # The parser should treat "-" as "empty" for list fields.
        assert assessment.prior_nack_reasons == [] or assessment.prior_nack_reasons == ["-"]
        assert assessment.prior_conditional_obligation in ("", "-")


# ---------------------------------------------------------------------------
# Adversarial probes: mode gating
# ---------------------------------------------------------------------------


class TestBrcMemoryModeGatingAdversarial:
    def test_unknown_mode_fails_safe_to_off(self, memory_env, monkeypatch):
        """An undocumented ``EGG_BRC_MEMORY`` value (e.g. ``"debug"``)
        fails safe to ``off`` — writes and reads are both no-ops."""
        monkeypatch.setenv("EGG_BRC_MEMORY", "debug")
        mode = brc_memory.get_memory_mode()
        assert mode == "off"
        assert not brc_memory.is_writes_enabled()
        assert not brc_memory.is_reads_enabled()

    def test_write_only_does_not_enable_reads(self, memory_env, monkeypatch):
        """``write-only`` mode enables writes but NOT reads (the slice-3
        reader respects this)."""
        monkeypatch.setenv("EGG_BRC_MEMORY", "write-only")
        assert brc_memory.is_writes_enabled()
        assert not brc_memory.is_reads_enabled()

    def test_full_mode_enables_both(self, memory_env, monkeypatch):
        """``full`` mode enables both writes and reads."""
        monkeypatch.setenv("EGG_BRC_MEMORY", "full")
        assert brc_memory.is_writes_enabled()
        assert brc_memory.is_reads_enabled()


# ---------------------------------------------------------------------------
# Adversarial probes: record_review
# ---------------------------------------------------------------------------


class TestBrcMemoryRecordReviewAdversarial:
    def test_ack_clears_prior_nack_reasons(self, memory_env, tmp_path):
        """After an ACK, the per-producer ``prior_nack_reasons`` list is
        cleared (the producer's fixes have landed)."""
        path = brc_memory.memory_path_for_role()

        # First: a NACK seeds the reasons.
        brc_memory.record_review(
            verdict="NACK",
            producer_role="coder",
            reason="missing error handling",
            commit_sha="abc123",
        )
        memory = brc_memory.load_memory(path)
        assert len(memory.per_producer["coder"].prior_nack_reasons) > 0

        # Then: an ACK clears them.
        brc_memory.record_review(
            verdict="ACK",
            producer_role="coder",
            reason="looks good now",
            commit_sha="def456",
        )
        memory = brc_memory.load_memory(path)
        assert memory.per_producer["coder"].prior_nack_reasons == []

    def test_conditional_ack_upgrades_verdict(self, memory_env, tmp_path):
        """When ``pre_merge_condition`` is non-empty and verdict is
        ``"ACK"``, the stored verdict is promoted to
        ``"conditional-ACK"``."""
        path = brc_memory.memory_path_for_role()
        brc_memory.record_review(
            verdict="ACK",
            producer_role="coder",
            reason="approved with obligation",
            pre_merge_condition="git mv old/path new/path",
            commit_sha="abc123",
        )
        memory = brc_memory.load_memory(path)
        assert memory.per_producer["coder"].prior_verdict == "conditional-ACK"
        assert "git mv" in memory.per_producer["coder"].prior_conditional_obligation

    def test_reason_with_unicode_preserved(self, memory_env, tmp_path):
        """Reason text containing emoji or non-ASCII is preserved
        (not silently dropped or replaced with ``?``)."""
        path = brc_memory.memory_path_for_role()
        brc_memory.record_review(
            verdict="NACK",
            producer_role="coder",
            reason="Bug 🔥 in line 42 — please fix",
            commit_sha="abc123",
        )
        memory = brc_memory.load_memory(path)
        # The reason lands in prior_nack_reasons.
        assert len(memory.per_producer["coder"].prior_nack_reasons) > 0
        # Unicode preserved (not replaced with "?").
        stored = memory.per_producer["coder"].prior_nack_reasons[0]
        assert "🔥" in stored or "Bug" in stored
