"""Tests for ``egg_overseer.state`` (issue #1962).

Covers:
* ``compute_anomaly_signature`` determinism + Tier-1 sort independence.
* ``FiledIssueRecord`` field semantics (issue_number optional on skip).
* ``append_filed_issue`` / ``load_filed_issues`` JSONL roundtrip with
  header validation, malformed-line tolerance, and schema-version
  enforcement.
* ``load_agent_timing`` / ``save_agent_timing`` JSON atomic-write
  roundtrip.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from egg_overseer.state import (
    AgentTimingEntry,
    AgentTimingState,
    FiledIssueRecord,
    append_filed_issue,
    compute_anomaly_signature,
    load_agent_timing,
    load_filed_issues,
    save_agent_timing,
)

# ---------------------------------------------------------------------------
# compute_anomaly_signature
# ---------------------------------------------------------------------------


class TestComputeAnomalySignature:
    def test_signature_is_16_hex(self) -> None:
        sig = compute_anomaly_signature("agent-loop", "coder", "owner/repo")
        assert len(sig) == 16
        int(sig, 16)  # must parse as hex

    def test_deterministic_same_inputs(self) -> None:
        a = compute_anomaly_signature("agent-loop", "coder", "owner/repo")
        b = compute_anomaly_signature("agent-loop", "coder", "owner/repo")
        assert a == b

    def test_different_anomaly_type_yields_different_signature(self) -> None:
        a = compute_anomaly_signature("agent-loop", "coder", "owner/repo")
        b = compute_anomaly_signature("agent-stall", "coder", "owner/repo")
        assert a != b

    def test_different_role_yields_different_signature(self) -> None:
        a = compute_anomaly_signature("agent-loop", "coder", "owner/repo")
        b = compute_anomaly_signature("agent-loop", "tester", "owner/repo")
        assert a != b

    def test_different_repo_yields_different_signature(self) -> None:
        a = compute_anomaly_signature("agent-loop", "coder", "owner/repo")
        b = compute_anomaly_signature("agent-loop", "coder", "owner/other")
        assert a != b

    def test_tier1_alerts_order_independent(self) -> None:
        a = compute_anomaly_signature(
            "agent-loop",
            "coder",
            "owner/repo",
            tier1_alert_types=("a", "b"),
        )
        b = compute_anomaly_signature(
            "agent-loop",
            "coder",
            "owner/repo",
            tier1_alert_types=("b", "a"),
        )
        assert a == b

    def test_first_8_hex_substring_stable(self) -> None:
        # The CLI embeds the first 8 chars in titles; a refactor that
        # changes prefix length breaks gh search dedup.
        sig = compute_anomaly_signature("agent-loop", "coder", "owner/repo")
        assert sig[:8].isalnum()
        assert len(sig[:8]) == 8


# ---------------------------------------------------------------------------
# FiledIssueRecord
# ---------------------------------------------------------------------------


class TestFiledIssueRecord:
    def _record(self, **overrides: object) -> FiledIssueRecord:
        base: dict[str, object] = {
            "issue_number": 123,
            "anomaly_type": "agent-loop",
            "anomaly_signature": "abc1234567890def",
            "agent_role": "coder",
            "repo": "owner/repo",
            "pipeline_id": "issue-1",
            "phase": "implement",
            "filed_at": datetime.now(UTC),
            "parent_alert_message_id": "msg-1",
            "hitl_outcome": "filed",
        }
        base.update(overrides)
        return FiledIssueRecord(**base)  # type: ignore[arg-type]

    def test_filed_record_round_trip(self) -> None:
        rec = self._record()
        payload = rec.model_dump_json()
        rebuilt = FiledIssueRecord.model_validate_json(payload)
        assert rebuilt == rec

    def test_skipped_record_with_no_issue_number(self) -> None:
        rec = self._record(issue_number=None, hitl_outcome="skipped")
        payload = rec.model_dump_json()
        rebuilt = FiledIssueRecord.model_validate_json(payload)
        assert rebuilt.issue_number is None
        assert rebuilt.hitl_outcome == "skipped"

    def test_invalid_hitl_outcome_rejected(self) -> None:
        with pytest.raises(ValueError):
            self._record(hitl_outcome="garbage")

    def test_pipeline_id_required(self) -> None:
        with pytest.raises(ValueError):
            FiledIssueRecord(
                anomaly_type="x",
                anomaly_signature="abcd",
                agent_role="r",
                repo="o/r",
                phase="implement",
                filed_at=datetime.now(UTC),
                # pipeline_id missing
            )  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# append_filed_issue / load_filed_issues
# ---------------------------------------------------------------------------


class TestFiledIssuesJsonl:
    def _record(self, sig: str, num: int) -> FiledIssueRecord:
        return FiledIssueRecord(
            issue_number=num,
            anomaly_type="agent-loop",
            anomaly_signature=sig,
            agent_role="coder",
            repo="owner/repo",
            pipeline_id="issue-1",
            phase="implement",
            filed_at=datetime(2026, 1, 1, tzinfo=UTC),
            parent_alert_message_id="msg-1",
            hitl_outcome="filed",
        )

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.jsonl"
        assert load_filed_issues(path) == []

    def test_append_creates_header_then_record(self, tmp_path: Path) -> None:
        path = tmp_path / "filed-issues.jsonl"
        rec = self._record("sig1", 100)
        append_filed_issue(path, rec)
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        header = json.loads(lines[0])
        assert header == {"_kind": "header", "schema_version": 1}
        assert json.loads(lines[1])["issue_number"] == 100

    def test_append_idempotent_header(self, tmp_path: Path) -> None:
        path = tmp_path / "f.jsonl"
        append_filed_issue(path, self._record("sig1", 1))
        append_filed_issue(path, self._record("sig2", 2))
        lines = path.read_text(encoding="utf-8").splitlines()
        # Header appears exactly once even after multiple appends.
        assert sum(1 for line in lines if '"_kind": "header"' in line) == 1
        assert len(lines) == 3

    def test_round_trip_load(self, tmp_path: Path) -> None:
        path = tmp_path / "f.jsonl"
        recs = [self._record(f"sig{i}", i) for i in (1, 2, 3)]
        for r in recs:
            append_filed_issue(path, r)
        loaded = load_filed_issues(path)
        assert [r.issue_number for r in loaded] == [1, 2, 3]
        assert [r.anomaly_signature for r in loaded] == ["sig1", "sig2", "sig3"]

    def test_malformed_line_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "f.jsonl"
        append_filed_issue(path, self._record("sig1", 1))
        # Corrupt an extra line manually.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{not valid json\n")
        append_filed_issue(path, self._record("sig2", 2))
        loaded = load_filed_issues(path)
        # Malformed line is skipped, well-formed records returned.
        assert [r.issue_number for r in loaded] == [1, 2]

    def test_missing_header_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "f.jsonl"
        path.write_text(
            json.dumps({"issue_number": 1, "anomaly_type": "x"}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="header"):
            load_filed_issues(path)

    def test_invalid_json_header_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "f.jsonl"
        path.write_text("garbage\n", encoding="utf-8")
        with pytest.raises(ValueError, match="header"):
            load_filed_issues(path)

    def test_unknown_schema_version_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "f.jsonl"
        path.write_text(
            json.dumps({"_kind": "header", "schema_version": 99}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="schema_version"):
            load_filed_issues(path)

    def test_blank_first_line_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "f.jsonl"
        path.write_text("\n", encoding="utf-8")
        assert load_filed_issues(path) == []


# ---------------------------------------------------------------------------
# AgentTimingState (load + save with flock)
# ---------------------------------------------------------------------------


class TestAgentTimingState:
    def _entry(self, role: str = "coder") -> AgentTimingEntry:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return AgentTimingEntry(
            role=role,
            phase="implement",
            phase_entered_at=now,
            first_seen_at=now,
            has_any_messages=True,
            alerted_anomalies={"agent-stall": now},
        )

    def test_load_missing_returns_default_state(self, tmp_path: Path) -> None:
        path = tmp_path / "agent-timing.json"
        state = load_agent_timing(path, pipeline_id="issue-9")
        assert state.pipeline_id == "issue-9"
        assert state.entries == {}
        assert state.schema_version == 1

    def test_save_then_load_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "agent-timing.json"
        state = AgentTimingState(
            pipeline_id="issue-1",
            entries={"coder": self._entry()},
        )
        save_agent_timing(state, path)
        rebuilt = load_agent_timing(path)
        assert rebuilt.pipeline_id == "issue-1"
        assert "coder" in rebuilt.entries
        assert rebuilt.entries["coder"].alerted_anomalies == {
            "agent-stall": datetime(2026, 1, 1, tzinfo=UTC)
        }

    def test_save_writes_atomically_via_rename(self, tmp_path: Path) -> None:
        # save_agent_timing must not leave a partial file in place if
        # interrupted between flush and rename.
        path = tmp_path / "agent-timing.json"
        state = AgentTimingState(
            pipeline_id="issue-1",
            entries={"a": self._entry("a")},
        )
        save_agent_timing(state, path)
        # No leftover .tmp files from the NamedTemporaryFile.
        leftovers = [
            p
            for p in tmp_path.iterdir()
            if p.name.endswith(".tmp") and p.name.startswith(".agent-timing.json.")
        ]
        assert leftovers == []

    def test_save_creates_lock_sentinel(self, tmp_path: Path) -> None:
        path = tmp_path / "agent-timing.json"
        save_agent_timing(AgentTimingState(pipeline_id="issue-1"), path)
        assert (tmp_path / "agent-timing.json.lock").exists()

    def test_concurrent_save_does_not_corrupt(self, tmp_path: Path) -> None:
        # The flock should serialize concurrent writes so the on-disk
        # JSON is never mid-write.
        path = tmp_path / "agent-timing.json"

        def writer(role: str) -> None:
            state = AgentTimingState(
                pipeline_id="issue-1",
                entries={role: self._entry(role)},
            )
            for _ in range(5):
                save_agent_timing(state, path)

        threads = [threading.Thread(target=writer, args=(f"r{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # File parses cleanly after all the concurrent writes.
        rebuilt = load_agent_timing(path)
        assert rebuilt.pipeline_id == "issue-1"
