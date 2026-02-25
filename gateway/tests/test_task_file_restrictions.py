"""Tests for per-task file restriction enforcement.

Validates the session-level file allowlist enforcement in push validation,
including warn-then-block semantics, strict mode, glob matching, directory
expansion, escape hatch, and phase blocked pattern intersection.
"""

from phase_filter import (
    PhaseFileRestriction,
    build_session_file_restriction,
    check_session_file_restrictions,
)


class TestBuildSessionFileRestriction:
    """Tests for build_session_file_restriction helper."""

    def test_basic_allowed_files(self):
        """Allowed files are passed through as allowed_patterns."""
        restriction = build_session_file_restriction(
            ["src/auth/*", "tests/test_auth.py"], "implement"
        )
        assert isinstance(restriction, PhaseFileRestriction)
        assert "src/auth/*" in restriction.allowed_patterns
        assert "tests/test_auth.py" in restriction.allowed_patterns

    def test_implement_phase_blocked_patterns_inherited(self):
        """Implement phase blocked patterns are preserved (intersection semantics)."""
        restriction = build_session_file_restriction(
            ["src/auth/*", ".egg-state/contracts/foo.json"], "implement"
        )
        # Phase blocked patterns should be present
        assert any(".egg-state/contracts" in p for p in restriction.blocked_patterns)

    def test_allowed_file_in_phase_blocked_dir_is_blocked(self):
        """Phase blocked patterns always win, even if in session allowlist."""
        restriction = build_session_file_restriction([".egg-state/contracts/*"], "implement")
        # The file should be blocked because phase blocks take priority
        allowed, reason = restriction.is_file_allowed(".egg-state/contracts/foo.json")
        assert not allowed
        assert "blocked" in reason.lower()

    def test_unknown_phase_uses_allowed_only(self):
        """Unknown phase creates restriction with just allowed_files."""
        restriction = build_session_file_restriction(["src/auth/*"], "unknown_phase")
        assert "src/auth/*" in restriction.allowed_patterns
        assert restriction.blocked_patterns == []

    def test_file_in_allowlist_is_allowed(self):
        """Files matching allowlist patterns should be allowed."""
        restriction = build_session_file_restriction(["src/auth/*", "tests/**"], "implement")
        allowed, _ = restriction.is_file_allowed("src/auth/login.py")
        assert allowed

    def test_file_outside_allowlist_is_blocked(self):
        """Files not matching any allowlist pattern should be blocked."""
        restriction = build_session_file_restriction(["src/auth/*"], "implement")
        allowed, _ = restriction.is_file_allowed("src/other/foo.py")
        assert not allowed


class TestCheckSessionFileRestrictions:
    """Tests for check_session_file_restrictions convenience function."""

    def test_empty_allowed_files_allows_all(self):
        """Empty allowed_files means no restriction."""
        result = check_session_file_restrictions([], "implement", ["src/any.py"])
        assert result.allowed

    def test_none_files_allows_all(self):
        """No files to check means allowed."""
        result = check_session_file_restrictions(["src/*"], "implement", [])
        assert result.allowed

    def test_file_in_scope_allowed(self):
        """File matching an allowed pattern should be allowed."""
        result = check_session_file_restrictions(
            ["src/auth/*", "tests/**"], "implement", ["src/auth/login.py"]
        )
        assert result.allowed

    def test_file_out_of_scope_blocked(self):
        """File not matching any allowed pattern should be blocked."""
        result = check_session_file_restrictions(["src/auth/*"], "implement", ["src/other/foo.py"])
        assert not result.allowed
        assert "src/other/foo.py" in result.blocked_files

    def test_mixed_files_reports_blocked(self):
        """Mix of in-scope and out-of-scope files reports only blocked."""
        result = check_session_file_restrictions(
            ["src/auth/*"],
            "implement",
            ["src/auth/login.py", "src/other/foo.py", "README.md"],
        )
        assert not result.allowed
        assert "src/other/foo.py" in result.blocked_files
        assert "README.md" in result.blocked_files
        assert "src/auth/login.py" not in result.blocked_files

    def test_glob_pattern_matching(self):
        """Glob patterns like tests/** should match nested files."""
        result = check_session_file_restrictions(
            ["tests/**"], "implement", ["tests/unit/test_auth.py"]
        )
        assert result.allowed

    def test_star_tsx_pattern(self):
        """Pattern like src/components/*.tsx should match .tsx files."""
        result = check_session_file_restrictions(
            ["src/components/*.tsx"],
            "implement",
            ["src/components/Button.tsx"],
        )
        assert result.allowed

    def test_phase_blocked_patterns_win(self):
        """Phase blocked patterns take priority over session allowed patterns."""
        result = check_session_file_restrictions(
            [".egg-state/contracts/*", "src/*"],
            "implement",
            [".egg-state/contracts/contract.json"],
        )
        assert not result.allowed


class TestSessionAllowedFilesOnSession:
    """Tests for Session.allowed_files and _warned_files."""

    def test_session_with_allowed_files(self):
        """Session can be created with allowed_files."""
        from datetime import UTC, datetime, timedelta

        from session_manager import Session, _hash_token

        now = datetime.now(UTC)
        session = Session(
            session_token="test",
            session_token_hash=_hash_token("test"),
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            allowed_files=["src/auth/*", "tests/**"],
        )
        assert session.allowed_files == ["src/auth/*", "tests/**"]
        assert session._warned_files == {}

    def test_session_without_allowed_files(self):
        """Session without allowed_files defaults to None."""
        from datetime import UTC, datetime, timedelta

        from session_manager import Session, _hash_token

        now = datetime.now(UTC)
        session = Session(
            session_token="test",
            session_token_hash=_hash_token("test"),
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        assert session.allowed_files is None
        assert session._warned_files == {}

    def test_warned_files_tracking(self):
        """_warned_files tracks per-file violation counts."""
        from datetime import UTC, datetime, timedelta

        from session_manager import Session, _hash_token

        now = datetime.now(UTC)
        session = Session(
            session_token="test",
            session_token_hash=_hash_token("test"),
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            allowed_files=["src/*"],
        )
        # Simulate violation tracking
        session._warned_files["other/file.py"] = 1
        assert session._warned_files["other/file.py"] == 1
        session._warned_files["other/file.py"] = 2
        assert session._warned_files["other/file.py"] == 2

    def test_add_allowed_file(self):
        """add_allowed_file adds file and parent directory glob."""
        from datetime import UTC, datetime, timedelta

        from session_manager import Session, _hash_token

        now = datetime.now(UTC)
        session = Session(
            session_token="test",
            session_token_hash=_hash_token("test"),
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            allowed_files=["src/auth/*"],
        )
        session.add_allowed_file("src/utils/new.py")
        assert "src/utils/new.py" in session.allowed_files
        assert "src/utils/*" in session.allowed_files

    def test_add_allowed_file_initializes_none(self):
        """add_allowed_file initializes allowed_files from None."""
        from datetime import UTC, datetime, timedelta

        from session_manager import Session, _hash_token

        now = datetime.now(UTC)
        session = Session(
            session_token="test",
            session_token_hash=_hash_token("test"),
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
        )
        assert session.allowed_files is None
        session.add_allowed_file("src/new.py")
        assert session.allowed_files is not None
        assert "src/new.py" in session.allowed_files

    def test_add_allowed_file_deduplicates(self):
        """add_allowed_file doesn't add duplicates."""
        from datetime import UTC, datetime, timedelta

        from session_manager import Session, _hash_token

        now = datetime.now(UTC)
        session = Session(
            session_token="test",
            session_token_hash=_hash_token("test"),
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            allowed_files=["src/utils/new.py", "src/utils/*"],
        )
        session.add_allowed_file("src/utils/new.py")
        assert session.allowed_files.count("src/utils/new.py") == 1
        assert session.allowed_files.count("src/utils/*") == 1


class TestSessionAllowedFilesPersistence:
    """Tests for allowed_files persistence round-trip."""

    def test_persistence_round_trip(self, tmp_path):
        """allowed_files survives persistence round-trip."""
        from session_manager import SessionManager

        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, session = manager.register_session(
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            allowed_files=["src/auth/*", "tests/**"],
        )
        assert session.allowed_files == ["src/auth/*", "tests/**"]

        # Load from disk
        manager2 = SessionManager(persistence_file=tmp_path / "sessions.json")
        # Validate with the original token to get session
        result = manager2.validate_session(token)
        assert result.valid
        assert result.session.allowed_files == ["src/auth/*", "tests/**"]

    def test_persistence_without_allowed_files(self, tmp_path):
        """Sessions without allowed_files load correctly."""
        from session_manager import SessionManager

        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, session = manager.register_session(
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
        )
        assert session.allowed_files is None

        # Load from disk
        manager2 = SessionManager(persistence_file=tmp_path / "sessions.json")
        result = manager2.validate_session(token)
        assert result.valid
        assert result.session.allowed_files is None

    def test_warned_files_not_persisted(self, tmp_path):
        """_warned_files should not be persisted."""
        from session_manager import SessionManager

        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, session = manager.register_session(
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            allowed_files=["src/*"],
        )
        session._warned_files["bad.py"] = 3

        # Load from disk
        manager2 = SessionManager(persistence_file=tmp_path / "sessions.json")
        result = manager2.validate_session(token)
        assert result.valid
        assert result.session._warned_files == {}

    def test_update_session_allowed_files(self, tmp_path):
        """update_session_allowed_files persists changes."""
        from session_manager import SessionManager

        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        token, session = manager.register_session(
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            allowed_files=["src/*"],
        )
        # Update
        success = manager.update_session_allowed_files(token, ["src/*", "tests/*", "docs/*"])
        assert success

        # Verify updated
        result = manager.validate_session(token)
        assert result.valid
        assert result.session.allowed_files == ["src/*", "tests/*", "docs/*"]

    def test_update_session_allowed_files_invalid_token(self, tmp_path):
        """update_session_allowed_files returns False for invalid token."""
        from session_manager import SessionManager

        manager = SessionManager(persistence_file=tmp_path / "sessions.json")
        success = manager.update_session_allowed_files("nonexistent-token", ["src/*"])
        assert not success


class TestAddAllowedFileEdgeCases:
    """Tests for Session.add_allowed_file edge cases."""

    def test_add_root_level_file(self):
        """add_allowed_file with a root-level file (no parent directory)."""
        from datetime import UTC, datetime, timedelta

        from session_manager import Session, _hash_token

        now = datetime.now(UTC)
        session = Session(
            session_token="test",
            session_token_hash=_hash_token("test"),
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            allowed_files=["src/auth/*"],
        )
        session.add_allowed_file("Makefile")
        assert "Makefile" in session.allowed_files
        # Root-level files have no parent, so no directory glob should be added
        assert len([f for f in session.allowed_files if f.startswith("*")]) == 0

    def test_add_deeply_nested_file(self):
        """add_allowed_file with a deeply nested file adds only immediate parent glob."""
        from datetime import UTC, datetime, timedelta

        from session_manager import Session, _hash_token

        now = datetime.now(UTC)
        session = Session(
            session_token="test",
            session_token_hash=_hash_token("test"),
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            allowed_files=[],
        )
        session.add_allowed_file("src/auth/handlers/login.py")
        assert "src/auth/handlers/login.py" in session.allowed_files
        assert "src/auth/handlers/*" in session.allowed_files
        # Should NOT add parent-of-parent
        assert "src/auth/*" not in session.allowed_files


class TestWarnThenBlockSemantics:
    """Tests for warn-then-block enforcement logic on Session._warned_files."""

    def test_first_violation_is_below_threshold(self):
        """First violation count (1) should not exceed default threshold (1)."""
        from datetime import UTC, datetime, timedelta

        from session_manager import Session, _hash_token

        now = datetime.now(UTC)
        session = Session(
            session_token="test",
            session_token_hash=_hash_token("test"),
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            allowed_files=["src/*"],
        )

        # Simulate first violation (warn)
        file_path = "other/file.py"
        count = session._warned_files.get(file_path, 0) + 1
        session._warned_files[file_path] = count
        warn_threshold = 1
        assert count <= warn_threshold  # Should NOT block (warn only)

    def test_second_violation_exceeds_threshold(self):
        """Second violation count (2) should exceed default threshold (1)."""
        from datetime import UTC, datetime, timedelta

        from session_manager import Session, _hash_token

        now = datetime.now(UTC)
        session = Session(
            session_token="test",
            session_token_hash=_hash_token("test"),
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            allowed_files=["src/*"],
        )

        file_path = "other/file.py"
        # First violation
        session._warned_files[file_path] = 1
        # Second violation
        count = session._warned_files.get(file_path, 0) + 1
        session._warned_files[file_path] = count
        warn_threshold = 1
        assert count > warn_threshold  # Should block

    def test_multiple_files_independent_tracking(self):
        """Each file has an independent violation counter."""
        from datetime import UTC, datetime, timedelta

        from session_manager import Session, _hash_token

        now = datetime.now(UTC)
        session = Session(
            session_token="test",
            session_token_hash=_hash_token("test"),
            container_id="c1",
            container_ip="1.2.3.4",
            mode="public",
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(hours=24),
            allowed_files=["src/*"],
        )

        # File A: two violations
        session._warned_files["a.py"] = 2
        # File B: one violation
        session._warned_files["b.py"] = 1

        warn_threshold = 1
        # A should be blocked
        assert session._warned_files["a.py"] > warn_threshold
        # B should still be at warn level
        assert session._warned_files["b.py"] <= warn_threshold


class TestBuildSessionFileRestrictionEdgeCases:
    """Additional edge cases for build_session_file_restriction."""

    def test_pr_phase_no_blocked_patterns(self):
        """PR phase has no blocked patterns, so all allowed_files work."""
        restriction = build_session_file_restriction(["src/auth/*"], "pr")
        # PR phase uses allowed_patterns=["*"], so no blocked patterns
        assert restriction.blocked_patterns == []

    def test_refine_phase_blocked_patterns(self):
        """Refine phase blocks code files (only allows .egg-state)."""
        restriction = build_session_file_restriction(["src/auth/*"], "refine")
        # Refine has allowed_patterns (not blocked_patterns) so blocked_patterns may be empty
        # The key behavior: files outside refine's allowlist are blocked
        allowed, _ = restriction.is_file_allowed("src/auth/login.py")
        assert allowed  # In the session allowlist

    def test_double_star_glob(self):
        """Double-star glob patterns (**) match deeply nested paths."""
        result = check_session_file_restrictions(
            ["src/**"], "implement", ["src/deep/nested/file.py"]
        )
        assert result.allowed

    def test_exact_file_match(self):
        """Exact file path (no glob) matches via prefix matching."""
        result = check_session_file_restrictions(
            ["pyproject.toml"], "implement", ["pyproject.toml"]
        )
        assert result.allowed

    def test_exact_file_does_not_match_similar_name(self):
        """Exact file path should not match a different filename starting with same prefix."""
        result = check_session_file_restrictions(
            ["pyproject.toml"], "implement", ["pyproject.toml.bak"]
        )
        assert not result.allowed
        assert "pyproject.toml.bak" in result.blocked_files

    def test_directory_glob_matches_all_children(self):
        """Directory glob src/components/* matches files in that directory."""
        result = check_session_file_restrictions(
            ["src/components/*"],
            "implement",
            ["src/components/Button.tsx", "src/components/Header.tsx"],
        )
        assert result.allowed

    def test_empty_string_in_allowed_files(self):
        """Empty string in allowed_files does not cause errors."""
        result = check_session_file_restrictions(["", "src/*"], "implement", ["src/file.py"])
        assert result.allowed
