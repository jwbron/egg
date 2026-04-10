"""Tests for egg_harness.result — AgentResult dataclass."""

from __future__ import annotations

import dataclasses

from egg_harness.result import AgentResult


class TestAgentResultFields:
    """Verify all expected fields exist with correct types."""

    def test_has_success_field(self):
        result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
        assert result.success is True

    def test_has_stdout_field(self):
        result = AgentResult(success=True, stdout="output text", stderr="", returncode=0)
        assert result.stdout == "output text"

    def test_has_stderr_field(self):
        result = AgentResult(success=False, stdout="", stderr="error msg", returncode=1)
        assert result.stderr == "error msg"

    def test_has_returncode_field(self):
        result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
        assert result.returncode == 0

    def test_has_error_field(self):
        result = AgentResult(
            success=False,
            stdout="",
            stderr="",
            returncode=1,
            error="something broke",
        )
        assert result.error == "something broke"

    def test_has_metadata_field(self):
        meta = {"model": "claude-opus-4-6", "provider": "anthropic"}
        result = AgentResult(
            success=True,
            stdout="ok",
            stderr="",
            returncode=0,
            metadata=meta,
        )
        assert result.metadata == meta

    def test_has_cost_usd_field(self):
        result = AgentResult(
            success=True,
            stdout="ok",
            stderr="",
            returncode=0,
            cost_usd=0.05,
        )
        assert result.cost_usd == 0.05

    def test_has_num_turns_field(self):
        result = AgentResult(
            success=True,
            stdout="ok",
            stderr="",
            returncode=0,
            num_turns=5,
        )
        assert result.num_turns == 5

    def test_has_duration_ms_field(self):
        result = AgentResult(
            success=True,
            stdout="ok",
            stderr="",
            returncode=0,
            duration_ms=12345,
        )
        assert result.duration_ms == 12345

    def test_has_session_id_field(self):
        result = AgentResult(
            success=True,
            stdout="ok",
            stderr="",
            returncode=0,
            session_id="sess_abc123",
        )
        assert result.session_id == "sess_abc123"

    def test_has_compaction_count_field(self):
        """New compaction_count field added in Phase 1."""
        result = AgentResult(
            success=True,
            stdout="ok",
            stderr="",
            returncode=0,
            compaction_count=3,
        )
        assert result.compaction_count == 3


class TestAgentResultDefaults:
    """Test default values for optional fields."""

    def test_error_defaults_to_none(self):
        result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
        assert result.error is None

    def test_metadata_defaults_to_none(self):
        result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
        assert result.metadata is None

    def test_cost_usd_defaults_to_none(self):
        result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
        assert result.cost_usd is None

    def test_num_turns_defaults_to_none(self):
        result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
        assert result.num_turns is None

    def test_duration_ms_defaults_to_none(self):
        result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
        assert result.duration_ms is None

    def test_session_id_defaults_to_none(self):
        result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
        assert result.session_id is None

    def test_compaction_count_defaults_to_none(self):
        """compaction_count should default to None for backward compat."""
        result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
        assert result.compaction_count is None


class TestAgentResultCreation:
    """Test creating AgentResult with minimal and maximal fields."""

    def test_create_with_only_required_fields(self):
        """Should be constructible with just the four required fields."""
        result = AgentResult(success=True, stdout="hello", stderr="", returncode=0)
        assert result.success is True
        assert result.stdout == "hello"
        assert result.stderr == ""
        assert result.returncode == 0

    def test_create_with_all_fields(self):
        """Should accept every field including the new compaction_count."""
        result = AgentResult(
            success=True,
            stdout="output",
            stderr="warnings",
            returncode=0,
            error=None,
            metadata={"key": "value"},
            cost_usd=1.23,
            num_turns=10,
            duration_ms=5000,
            session_id="sess_xyz",
            compaction_count=2,
        )
        assert result.success is True
        assert result.stdout == "output"
        assert result.stderr == "warnings"
        assert result.returncode == 0
        assert result.error is None
        assert result.metadata == {"key": "value"}
        assert result.cost_usd == 1.23
        assert result.num_turns == 10
        assert result.duration_ms == 5000
        assert result.session_id == "sess_xyz"
        assert result.compaction_count == 2

    def test_create_failure_result(self):
        """Typical failure result pattern."""
        result = AgentResult(
            success=False,
            stdout="",
            stderr="traceback...",
            returncode=1,
            error="Agent timed out",
            duration_ms=300_000,
        )
        assert result.success is False
        assert result.returncode == 1
        assert result.error == "Agent timed out"


class TestAgentResultDataclass:
    """Test that AgentResult is a well-formed dataclass."""

    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(AgentResult)

    def test_field_count(self):
        """Should have at least 11 fields (original 10 + compaction_count)."""
        fields = dataclasses.fields(AgentResult)
        assert len(fields) >= 11

    def test_field_names_include_all_expected(self):
        field_names = {f.name for f in dataclasses.fields(AgentResult)}
        expected = {
            "success",
            "stdout",
            "stderr",
            "returncode",
            "error",
            "metadata",
            "cost_usd",
            "num_turns",
            "duration_ms",
            "session_id",
            "compaction_count",
        }
        assert expected.issubset(field_names)


class TestAgentResultBackwardCompatibility:
    """Ensure backward compatibility with existing consumers."""

    def test_existing_code_pattern_still_works(self):
        """Simulate how existing code constructs AgentResult (no compaction)."""
        result = AgentResult(
            success=True,
            stdout="done",
            stderr="",
            returncode=0,
            cost_usd=0.01,
            num_turns=3,
            duration_ms=1500,
            session_id="sess_old",
        )
        # All existing fields accessible
        assert result.success is True
        assert result.cost_usd == 0.01
        assert result.session_id == "sess_old"
        # New field defaults to None — does not break existing consumers
        assert result.compaction_count is None

    def test_dict_conversion(self):
        """asdict should include all fields for serialization."""
        result = AgentResult(
            success=True,
            stdout="ok",
            stderr="",
            returncode=0,
            compaction_count=1,
        )
        d = dataclasses.asdict(result)
        assert "compaction_count" in d
        assert d["compaction_count"] == 1
        assert d["success"] is True

    def test_dict_conversion_without_compaction(self):
        """asdict with default compaction_count should have None."""
        result = AgentResult(success=True, stdout="ok", stderr="", returncode=0)
        d = dataclasses.asdict(result)
        assert "compaction_count" in d
        assert d["compaction_count"] is None


class TestAgentResultFieldTypes:
    """Verify field type annotations are correct."""

    def test_success_is_bool(self):
        result = AgentResult(success=True, stdout="", stderr="", returncode=0)
        assert isinstance(result.success, bool)

    def test_stdout_is_str(self):
        result = AgentResult(success=True, stdout="text", stderr="", returncode=0)
        assert isinstance(result.stdout, str)

    def test_stderr_is_str(self):
        result = AgentResult(success=True, stdout="", stderr="err", returncode=0)
        assert isinstance(result.stderr, str)

    def test_returncode_is_int(self):
        result = AgentResult(success=True, stdout="", stderr="", returncode=0)
        assert isinstance(result.returncode, int)

    def test_error_is_str_or_none(self):
        result = AgentResult(success=True, stdout="", stderr="", returncode=0, error="oops")
        assert isinstance(result.error, str)

    def test_metadata_is_dict_or_none(self):
        result = AgentResult(
            success=True,
            stdout="",
            stderr="",
            returncode=0,
            metadata={"k": "v"},
        )
        assert isinstance(result.metadata, dict)

    def test_cost_usd_is_float_or_none(self):
        result = AgentResult(success=True, stdout="", stderr="", returncode=0, cost_usd=0.5)
        assert isinstance(result.cost_usd, float)

    def test_num_turns_is_int_or_none(self):
        result = AgentResult(success=True, stdout="", stderr="", returncode=0, num_turns=3)
        assert isinstance(result.num_turns, int)

    def test_duration_ms_is_int_or_none(self):
        result = AgentResult(success=True, stdout="", stderr="", returncode=0, duration_ms=100)
        assert isinstance(result.duration_ms, int)

    def test_session_id_is_str_or_none(self):
        result = AgentResult(success=True, stdout="", stderr="", returncode=0, session_id="s1")
        assert isinstance(result.session_id, str)

    def test_compaction_count_is_int_or_none(self):
        result = AgentResult(
            success=True,
            stdout="",
            stderr="",
            returncode=0,
            compaction_count=5,
        )
        assert isinstance(result.compaction_count, int)
