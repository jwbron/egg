"""
Tests for ``gateway/confluence_policy.py``.

Covers Phase 1 / Task 4-3 acceptance:

- Allowlist round-trip from a tmp YAML using the ``confluence.spaces`` key.
- mtime-based reload picks up edits.
- ``reload_confluence_policy()`` clears the cache.
- Missing file / missing ``confluence:`` section / missing ``spaces:`` /
  non-list shape / malformed YAML → empty set (fail-closed) without crash.
- Mixed-case Atlassian space keys round-trip exactly.
- Non-string entries / invalid keys are dropped with a warning.
- ``is_space_allowed`` membership semantics.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import confluence_policy
import pytest
from confluence_policy import ConfluencePolicy


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body)


@pytest.fixture
def tmp_yaml(tmp_path: Path) -> Path:
    return tmp_path / "context-filters.yaml"


# ---------------------------------------------------------------------------
# Happy path — round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_round_trip_two_keys(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            """
confluence:
  spaces: ["ENG", "DOCS"]
""",
        )
        policy = ConfluencePolicy(tmp_yaml)
        assert policy.allowed_spaces() == frozenset({"ENG", "DOCS"})
        assert policy.is_space_allowed("ENG")
        assert policy.is_space_allowed("DOCS")
        assert not policy.is_space_allowed("LEAK")

    def test_empty_list_round_trip(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "confluence:\n  spaces: []\n")
        policy = ConfluencePolicy(tmp_yaml)
        assert policy.allowed_spaces() == frozenset()
        assert not policy.is_space_allowed("ENG")

    def test_mixed_case_keys_preserved(self, tmp_yaml: Path):
        """Atlassian space keys are case-sensitive — exact case must be kept."""
        _write_yaml(
            tmp_yaml,
            'confluence:\n  spaces: ["ENG", "docs", "My_Space1"]\n',
        )
        policy = ConfluencePolicy(tmp_yaml)
        spaces = policy.allowed_spaces()
        assert "ENG" in spaces
        assert "docs" in spaces
        assert "My_Space1" in spaces
        # Case must NOT collapse — `eng` is a different space.
        assert not policy.is_space_allowed("eng")
        assert not policy.is_space_allowed("DOCS")

    def test_is_space_allowed_rejects_blank(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, 'confluence:\n  spaces: [""]\n')
        policy = ConfluencePolicy(tmp_yaml)
        assert not policy.is_space_allowed("")
        assert not policy.is_space_allowed(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fail-closed semantics
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_missing_file_returns_empty_set(self, tmp_path: Path):
        policy = ConfluencePolicy(tmp_path / "no-such-file.yaml")
        assert policy.allowed_spaces() == frozenset()
        assert not policy.is_space_allowed("ENG")

    def test_missing_confluence_section(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, 'jira:\n  projects: ["ENG"]\n')
        policy = ConfluencePolicy(tmp_yaml)
        assert policy.allowed_spaces() == frozenset()

    def test_missing_spaces_key(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "confluence:\n  other: ignored\n")
        policy = ConfluencePolicy(tmp_yaml)
        assert policy.allowed_spaces() == frozenset()

    def test_non_list_spaces_shape(self, tmp_yaml: Path):
        """``confluence.spaces: ENG`` (string) must fail closed, not crash."""
        _write_yaml(tmp_yaml, "confluence:\n  spaces: ENG\n")
        policy = ConfluencePolicy(tmp_yaml)
        assert policy.allowed_spaces() == frozenset()

    def test_malformed_yaml_fails_closed(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "confluence:\n  spaces: [ENG, DOCS\n")  # unclosed list
        policy = ConfluencePolicy(tmp_yaml)
        # Must not raise — fails closed.
        assert policy.allowed_spaces() == frozenset()

    def test_top_level_non_mapping_fails_closed(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "- ENG\n- DOCS\n")
        policy = ConfluencePolicy(tmp_yaml)
        assert policy.allowed_spaces() == frozenset()

    def test_non_string_entries_dropped(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            'confluence:\n  spaces: ["ENG", 42, null, "DOCS"]\n',
        )
        policy = ConfluencePolicy(tmp_yaml)
        # Only ENG and DOCS survive validation.
        assert policy.allowed_spaces() == frozenset({"ENG", "DOCS"})

    def test_invalid_key_shape_dropped(self, tmp_yaml: Path):
        """Keys that don't match ``^[a-zA-Z][a-zA-Z0-9_]*$`` are dropped."""
        _write_yaml(
            tmp_yaml,
            'confluence:\n  spaces: ["ENG", "123BAD", "with-dash", "GOOD_KEY1"]\n',
        )
        policy = ConfluencePolicy(tmp_yaml)
        assert policy.allowed_spaces() == frozenset({"ENG", "GOOD_KEY1"})


# ---------------------------------------------------------------------------
# Cache + reload semantics
# ---------------------------------------------------------------------------


class TestCacheReload:
    def test_mtime_change_picks_up_edits(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, 'confluence:\n  spaces: ["ENG"]\n')
        policy = ConfluencePolicy(tmp_yaml)
        assert policy.allowed_spaces() == frozenset({"ENG"})

        time.sleep(0.05)
        _write_yaml(tmp_yaml, 'confluence:\n  spaces: ["ENG", "DOCS"]\n')
        new_mtime = time.time() + 1
        os.utime(tmp_yaml, (new_mtime, new_mtime))
        assert policy.allowed_spaces() == frozenset({"ENG", "DOCS"})

    def test_reload_forces_reread_without_mtime_change(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, 'confluence:\n  spaces: ["ENG"]\n')
        policy = ConfluencePolicy(tmp_yaml)
        assert policy.allowed_spaces() == frozenset({"ENG"})

        # Rewrite content while preserving mtime.
        old_mtime = policy._cached_mtime
        _write_yaml(tmp_yaml, 'confluence:\n  spaces: ["ENG", "DOCS"]\n')
        os.utime(tmp_yaml, (old_mtime, old_mtime))
        # Without reload, cache still has just ENG.
        assert policy.allowed_spaces() == frozenset({"ENG"})

        policy.reload()
        # After reload, the new content is picked up.
        assert policy.allowed_spaces() == frozenset({"ENG", "DOCS"})

    def test_disappearing_file_clears_cache(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, 'confluence:\n  spaces: ["ENG"]\n')
        policy = ConfluencePolicy(tmp_yaml)
        assert policy.allowed_spaces() == frozenset({"ENG"})

        tmp_yaml.unlink()
        # Subsequent calls must return an empty set, not the cached value.
        assert policy.allowed_spaces() == frozenset()


# ---------------------------------------------------------------------------
# Module-level singleton helpers
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_reload_helper_flushes_singleton(self, tmp_yaml: Path, monkeypatch):
        # Point the default config path at our tmp file.
        monkeypatch.setattr(
            confluence_policy,
            "_DEFAULT_CONFIG_PATH",
            tmp_yaml,
        )
        confluence_policy.reset_confluence_policy()
        _write_yaml(tmp_yaml, 'confluence:\n  spaces: ["ENG"]\n')
        assert confluence_policy.allowed_spaces() == frozenset({"ENG"})
        assert confluence_policy.is_space_allowed("ENG")

        _write_yaml(tmp_yaml, 'confluence:\n  spaces: ["ENG", "DOCS"]\n')
        # Keep mtime stable — the singleton must not be relying on mtime here.
        old_mtime = confluence_policy.get_confluence_policy()._cached_mtime
        os.utime(tmp_yaml, (old_mtime, old_mtime))
        confluence_policy.reload_confluence_policy()
        assert confluence_policy.allowed_spaces() == frozenset({"ENG", "DOCS"})

    def test_reset_clears_singleton(self):
        confluence_policy.reset_confluence_policy()
        assert confluence_policy._confluence_policy is None
