"""
Tests for gateway/jira_policy.py.

Covers:
- allowlist round-trip from a tmp ``context-filters.yaml`` using the
  authoritative ``jira.projects`` key
- mtime-based reload
- ``reload_jira_policy()`` forces a re-read
- fail-closed on missing file, missing ``jira:`` section, malformed YAML,
  wrong top-level shape, non-list ``projects`` value
- invalid project keys skipped (non-string, bad shape)
- ``extract_project_key`` on good / bad / non-string input
- module-level singleton + ``reset`` helper
- ``jira.link_types`` config knob (#1924): missing key → defaults; explicit
  list overrides; mtime-cache invalidation; case-sensitive lookup; fail-
  closed (defaults) on malformed input.
- ``jira.epic_link_field`` knob: missing key → ``"parent"`` default; valid
  values accepted; invalid values fall back to default.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# Modules loaded via conftest.py.
import jira_policy
import pytest
from jira_policy import (
    JiraPolicy,
    extract_project_key,
    reload_jira_policy,
    reset_jira_policy,
)
from jira_policy import (
    allowed_projects as allowed_projects_singleton,
)
from jira_policy import (
    is_project_allowed as is_allowed_singleton,
)


@pytest.fixture
def tmp_yaml(tmp_path: Path) -> Path:
    return tmp_path / "context-filters.yaml"


def _write_yaml(path: Path, content: str) -> None:
    path.write_text(content)


class TestAllowlistRoundTrip:
    def test_loads_projects_list(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG, DEVOPS]\n",
        )
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset({"ENG", "DEVOPS"})

    def test_empty_projects_list_is_empty_set(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira:\n  projects: []\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset()

    def test_is_project_allowed(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira:\n  projects: [ENG]\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.is_project_allowed("ENG") is True
        assert policy.is_project_allowed("SEC") is False
        assert policy.is_project_allowed("") is False

    def test_invalid_project_keys_skipped(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG, lowercase_bad, 'with space', '1starts_with_digit']\n",
        )
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset({"ENG"})

    def test_non_string_entry_skipped(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira:\n  projects: [ENG, 42]\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset({"ENG"})


class TestFailClosed:
    def test_missing_file(self, tmp_path: Path):
        policy = JiraPolicy(tmp_path / "does-not-exist.yaml")
        assert policy.allowed_projects() == frozenset()

    def test_missing_jira_section(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "other: stuff\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset()

    def test_jira_section_not_a_mapping(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira: not-a-mapping\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset()

    def test_projects_missing(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira: {}\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset()

    def test_projects_not_a_list(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira:\n  projects: ENG\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset()

    def test_malformed_yaml_does_not_crash(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira:\n  projects: [\n")
        policy = JiraPolicy(tmp_yaml)
        # Must return a frozenset (not raise).
        result = policy.allowed_projects()
        assert result == frozenset()

    def test_top_level_not_mapping(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "- just\n- a\n- list\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset()

    def test_empty_file(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "")
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset()


class TestCacheReload:
    def test_mtime_change_triggers_reload(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira:\n  projects: [ENG]\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset({"ENG"})

        # Bump the mtime and rewrite.
        new_mtime = tmp_yaml.stat().st_mtime + 2
        _write_yaml(tmp_yaml, "jira:\n  projects: [SEC]\n")
        os.utime(tmp_yaml, (new_mtime, new_mtime))

        assert policy.allowed_projects() == frozenset({"SEC"})

    def test_reload_clears_cache(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira:\n  projects: [ENG]\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset({"ENG"})

        # Rewrite WITHOUT changing mtime; cache holds old value.
        original_mtime = tmp_yaml.stat().st_mtime
        _write_yaml(tmp_yaml, "jira:\n  projects: [SEC]\n")
        os.utime(tmp_yaml, (original_mtime, original_mtime))
        time.sleep(0.001)
        assert policy.allowed_projects() == frozenset({"ENG"})

        policy.reload()
        assert policy.allowed_projects() == frozenset({"SEC"})

    def test_file_disappearing_clears_cache(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira:\n  projects: [ENG]\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.allowed_projects() == frozenset({"ENG"})

        tmp_yaml.unlink()
        assert policy.allowed_projects() == frozenset()


class TestExtractProjectKey:
    @pytest.mark.parametrize(
        "ticket, expected",
        [
            ("FOO-123", "FOO"),
            ("ENG-1", "ENG"),
            ("PROJ_X-42", "PROJ_X"),
            ("A1B2-9", "A1B2"),
            ("  ENG-1  ", "ENG"),  # surrounding whitespace tolerated
        ],
    )
    def test_valid_tickets(self, ticket: str, expected: str):
        assert extract_project_key(ticket) == expected

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "foo",
            "foo-1",  # lowercase not allowed
            "FOO",  # missing -<digits>
            "FOO-",  # missing digits
            "-123",  # missing project key
            "FOO-abc",  # non-digit trailing
        ],
    )
    def test_invalid_tickets_return_empty(self, bad: str):
        assert extract_project_key(bad) == ""

    def test_non_string_returns_empty(self):
        assert extract_project_key(None) == ""  # type: ignore[arg-type]
        assert extract_project_key(123) == ""  # type: ignore[arg-type]


class TestModuleSingleton:
    def test_reset_drops_singleton(self, tmp_yaml: Path, monkeypatch):
        _write_yaml(tmp_yaml, "jira:\n  projects: [ENG]\n")
        monkeypatch.setattr(jira_policy, "_DEFAULT_CONFIG_PATH", tmp_yaml)
        reset_jira_policy()
        first = jira_policy.get_jira_policy()
        # Point the freshly-created instance at our tmp file.
        first._config_path = tmp_yaml
        assert is_allowed_singleton("ENG") is True
        assert "ENG" in allowed_projects_singleton()

        reset_jira_policy()
        second = jira_policy.get_jira_policy()
        assert first is not second

    def test_reload_jira_policy_forces_reread(self, tmp_yaml: Path, monkeypatch):
        _write_yaml(tmp_yaml, "jira:\n  projects: [ENG]\n")
        monkeypatch.setattr(jira_policy, "_DEFAULT_CONFIG_PATH", tmp_yaml)
        reset_jira_policy()
        policy = jira_policy.get_jira_policy()
        policy._config_path = tmp_yaml
        assert allowed_projects_singleton() == frozenset({"ENG"})

        # Rewrite WITHOUT bumping mtime.
        original_mtime = tmp_yaml.stat().st_mtime
        _write_yaml(tmp_yaml, "jira:\n  projects: [SEC]\n")
        os.utime(tmp_yaml, (original_mtime, original_mtime))
        time.sleep(0.001)
        # Without reload we still see the old value.
        assert allowed_projects_singleton() == frozenset({"ENG"})

        reload_jira_policy()
        assert allowed_projects_singleton() == frozenset({"SEC"})


# -----------------------------------------------------------------------------
# link_types config knob (#1924)
#
# Refine decision-4: ``createIssueLink`` accepts only link types in the
# ``jira.link_types`` allowlist; default is ``["Blocks", "Relates"]``.
# Lookups are case-sensitive (Atlassian's link-type names are case-sensitive
# too).  Malformed values fail closed to defaults (never to "any").
# -----------------------------------------------------------------------------


class TestLinkTypes:
    def test_missing_key_uses_defaults(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira:\n  projects: [ENG]\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.link_types() == frozenset({"Blocks", "Relates"})

    def test_explicit_list_overrides_defaults(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types: [Cloners, Duplicate]\n",
        )
        policy = JiraPolicy(tmp_yaml)
        assert policy.link_types() == frozenset({"Cloners", "Duplicate"})

    def test_link_type_allowed_membership(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types: [Blocks]\n",
        )
        policy = JiraPolicy(tmp_yaml)
        assert policy.link_type_allowed("Blocks") is True
        assert policy.link_type_allowed("Relates") is False
        assert policy.link_type_allowed("Other") is False
        assert policy.link_type_allowed("") is False

    def test_link_type_allowed_case_sensitive(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types: [Blocks]\n",
        )
        policy = JiraPolicy(tmp_yaml)
        # Atlassian rejects mismatched case; the gateway must not silently
        # canonicalise.
        assert policy.link_type_allowed("Blocks") is True
        assert policy.link_type_allowed("blocks") is False
        assert policy.link_type_allowed("BLOCKS") is False

    def test_link_type_allowed_non_string_inputs_rejected(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types: [Blocks]\n",
        )
        policy = JiraPolicy(tmp_yaml)
        # Defence in depth — non-string callers (a route layer bug, say)
        # should be safely rejected.
        assert policy.link_type_allowed(None) is False  # type: ignore[arg-type]
        assert policy.link_type_allowed(123) is False  # type: ignore[arg-type]

    def test_explicit_empty_list_falls_back_to_defaults(self, tmp_yaml: Path):
        """Refine decision-4: an empty list is meaningless (would block ALL
        link creation).  The loader treats it as 'no override → use the
        default allowlist' so an operator can't accidentally lock everyone
        out by setting ``link_types: []``."""
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types: []\n",
        )
        policy = JiraPolicy(tmp_yaml)
        assert policy.link_types() == frozenset({"Blocks", "Relates"})

    def test_non_list_value_falls_back_to_defaults(self, tmp_yaml: Path):
        """A scalar / dict in place of a list is malformed; loader logs an
        error and uses defaults rather than crashing."""
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types: Blocks\n",
        )
        policy = JiraPolicy(tmp_yaml)
        assert policy.link_types() == frozenset({"Blocks", "Relates"})

        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types:\n    a: 1\n",
        )
        # Different mtime so the cache reloads.
        new_mtime = tmp_yaml.stat().st_mtime + 2
        os.utime(tmp_yaml, (new_mtime, new_mtime))
        assert policy.link_types() == frozenset({"Blocks", "Relates"})

    def test_non_string_entries_skipped(self, tmp_yaml: Path):
        """Mixed-type list — drop the bad entries, keep the good ones."""
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types: [Blocks, 42, Cloners]\n",
        )
        policy = JiraPolicy(tmp_yaml)
        # 42 dropped; Blocks + Cloners retained.
        assert policy.link_types() == frozenset({"Blocks", "Cloners"})

    def test_blank_strings_skipped(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types: [Blocks, '', '   ']\n",
        )
        policy = JiraPolicy(tmp_yaml)
        # Empty / whitespace-only entries are filtered; Blocks survives.
        assert policy.link_types() == frozenset({"Blocks"})

    def test_mtime_cache_invalidation(self, tmp_yaml: Path):
        """Operator edits the YAML; on the next ``link_types()`` call the
        new value is observed (mtime-driven reload)."""
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types: [Blocks]\n",
        )
        policy = JiraPolicy(tmp_yaml)
        assert policy.link_types() == frozenset({"Blocks"})

        new_mtime = tmp_yaml.stat().st_mtime + 2
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types: [Cloners, Relates]\n",
        )
        os.utime(tmp_yaml, (new_mtime, new_mtime))
        assert policy.link_types() == frozenset({"Cloners", "Relates"})

    def test_disappearing_file_returns_defaults(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types: [Cloners]\n",
        )
        policy = JiraPolicy(tmp_yaml)
        assert policy.link_types() == frozenset({"Cloners"})

        tmp_yaml.unlink()
        # File gone — projects clears, link_types returns to defaults.
        assert policy.link_types() == frozenset({"Blocks", "Relates"})
        assert policy.allowed_projects() == frozenset()

    def test_malformed_yaml_returns_defaults(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira:\n  projects: [\n")  # truncated list
        policy = JiraPolicy(tmp_yaml)
        # Parse error → fail closed for projects, defaults for link_types.
        assert policy.link_types() == frozenset({"Blocks", "Relates"})


# -----------------------------------------------------------------------------
# epic_link_field config knob (#1924)
#
# Refine decision-2: ``createJiraIssue``'s ``epicLink`` shorthand emits
# whichever Atlassian field this knob names.  ``"parent"`` (next-gen) is
# the default; ``"customfield_10014"`` is the classic / team-managed
# variant.  Anything else is malformed and falls back to the default.
# -----------------------------------------------------------------------------


class TestEpicLinkField:
    def test_missing_key_uses_default(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira:\n  projects: [ENG]\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.epic_link_field() == "parent"

    def test_parent_value_accepted(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  epic_link_field: parent\n",
        )
        policy = JiraPolicy(tmp_yaml)
        assert policy.epic_link_field() == "parent"

    def test_customfield_value_accepted(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  epic_link_field: customfield_10014\n",
        )
        policy = JiraPolicy(tmp_yaml)
        assert policy.epic_link_field() == "customfield_10014"

    def test_unknown_value_falls_back_to_default(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  epic_link_field: customfield_99999\n",
        )
        policy = JiraPolicy(tmp_yaml)
        # Refine decision-2 pinned the allowlist to {"parent",
        # "customfield_10014"}; anything else is malformed and falls back.
        assert policy.epic_link_field() == "parent"

    def test_non_string_value_falls_back_to_default(self, tmp_yaml: Path):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  epic_link_field: 42\n",
        )
        policy = JiraPolicy(tmp_yaml)
        assert policy.epic_link_field() == "parent"

    def test_mtime_cache_invalidation(self, tmp_yaml: Path):
        _write_yaml(tmp_yaml, "jira:\n  projects: [ENG]\n")
        policy = JiraPolicy(tmp_yaml)
        assert policy.epic_link_field() == "parent"

        new_mtime = tmp_yaml.stat().st_mtime + 2
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  epic_link_field: customfield_10014\n",
        )
        os.utime(tmp_yaml, (new_mtime, new_mtime))
        assert policy.epic_link_field() == "customfield_10014"

    def test_singleton_accessor_matches_instance(self, tmp_yaml: Path, monkeypatch):
        _write_yaml(
            tmp_yaml,
            "jira:\n  projects: [ENG]\n  link_types: [Blocks]\n  epic_link_field: customfield_10014\n",
        )
        monkeypatch.setattr(jira_policy, "_DEFAULT_CONFIG_PATH", tmp_yaml)
        reset_jira_policy()
        policy = jira_policy.get_jira_policy()
        policy._config_path = tmp_yaml
        assert jira_policy.link_types() == frozenset({"Blocks"})
        assert jira_policy.link_type_allowed("Blocks") is True
        assert jira_policy.link_type_allowed("Relates") is False
        assert jira_policy.epic_link_field() == "customfield_10014"
