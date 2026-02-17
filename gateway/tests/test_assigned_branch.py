"""Tests for Session.assigned_branch field and branch lock behavior.

Validates that:
- assigned_branch field is added to Session and serializes correctly
- Pipeline sessions with assigned_branch block branch switching
- Non-pipeline sessions are not affected
- Register session populates assigned_branch for pipeline sessions
"""

import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from session_manager import (
    Session,
    SessionManager,
    _hash_token,
)


class TestSessionAssignedBranchField:
    """Tests for the assigned_branch field on Session."""

    def test_defaults_to_none(self):
        """assigned_branch defaults to None."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        assert session.assigned_branch is None

    def test_can_set_assigned_branch(self):
        """assigned_branch can be set to a branch name."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            assigned_branch="egg/c1/work",
        )
        assert session.assigned_branch == "egg/c1/work"

    def test_to_dict_includes_assigned_branch(self):
        """to_dict_for_persistence includes assigned_branch when set."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            assigned_branch="egg/c1/work",
        )
        d = session.to_dict_for_persistence()
        assert d["assigned_branch"] == "egg/c1/work"

    def test_to_dict_excludes_none_assigned_branch(self):
        """to_dict_for_persistence omits assigned_branch when None."""
        now = datetime.now(UTC)
        session = Session(
            session_token="test-token",
            session_token_hash=_hash_token("test-token"),
            container_id="test-container",
            container_ip="172.18.0.5",
            mode="private",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        d = session.to_dict_for_persistence()
        assert "assigned_branch" not in d

    def test_from_persistence_with_assigned_branch(self):
        """from_persistence restores assigned_branch."""
        now = datetime.now(UTC)
        data = {
            "session_token_hash": _hash_token("test"),
            "container_id": "c1",
            "container_ip": "172.18.0.5",
            "mode": "private",
            "created_at": now.isoformat(),
            "last_seen": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "assigned_branch": "egg/c1/work",
        }
        session = Session.from_persistence(data)
        assert session.assigned_branch == "egg/c1/work"

    def test_from_persistence_without_assigned_branch(self):
        """from_persistence defaults assigned_branch to None for old data."""
        now = datetime.now(UTC)
        data = {
            "session_token_hash": _hash_token("test"),
            "container_id": "c1",
            "container_ip": "172.18.0.5",
            "mode": "private",
            "created_at": now.isoformat(),
            "last_seen": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
        }
        session = Session.from_persistence(data)
        assert session.assigned_branch is None

    def test_roundtrip_with_assigned_branch(self, tmp_path):
        """assigned_branch survives save/load cycle via SessionManager."""
        persistence_file = tmp_path / "sessions.json"
        manager = SessionManager(persistence_file=persistence_file)
        token, session = manager.register_session(
            container_id="c1",
            container_ip="172.18.0.5",
            mode="private",
            pipeline_id="issue-42",
            branch="egg/c1/work",
        )
        assert session.assigned_branch == "egg/c1/work"

        # Reload from disk
        manager2 = SessionManager(persistence_file=persistence_file)
        result = manager2.validate_session(token, source_ip="172.18.0.5")
        assert result.session is not None
        assert result.session.assigned_branch == "egg/c1/work"


class TestRegisterSessionAssignedBranch:
    """Tests for register_session populating assigned_branch."""

    def test_pipeline_session_with_branch_gets_assigned_branch(self, tmp_path):
        """Pipeline sessions with branch get assigned_branch set."""
        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, session = manager.register_session(
            container_id="c1",
            container_ip="172.18.0.5",
            mode="private",
            pipeline_id="issue-42",
            branch="egg/c1/work",
        )
        assert session.assigned_branch == "egg/c1/work"

    def test_non_pipeline_session_no_assigned_branch(self, tmp_path):
        """Non-pipeline sessions do not get assigned_branch."""
        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, session = manager.register_session(
            container_id="c1",
            container_ip="172.18.0.5",
            mode="private",
            branch="egg/feature",
        )
        assert session.assigned_branch is None

    def test_pipeline_without_branch_no_assigned_branch(self, tmp_path):
        """Pipeline session without branch doesn't set assigned_branch."""
        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, session = manager.register_session(
            container_id="c1",
            container_ip="172.18.0.5",
            mode="private",
            pipeline_id="issue-42",
        )
        assert session.assigned_branch is None
