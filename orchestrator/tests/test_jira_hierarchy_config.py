"""Tests for orchestrator.jira_hierarchy_config (#1557 TASK-1-4).

Covers:
- YAML loading happy paths (``parent`` / ``epic_link`` per project).
- Missing-file / empty-file / comment-only behaviour.
- Malformed YAML raises :class:`JiraHierarchyConfigError`.
- Schema validators reject non-mapping bodies, non-string keys, empty keys,
  unknown hierarchy values, and non-string values.
- mtime caching: same mtime skips reload; new mtime triggers reload;
  missing-then-created promotes the empty mapping to the parsed file.
- Thread-safe ``get_config()`` smoke test.
- ``resolve_hierarchy_field`` raises :class:`JiraHierarchyUnmappedError`
  with the active manager's path baked into the message.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from pathlib import Path

# ``orchestrator/`` is added to sys.path by orchestrator/tests/conftest.py so
# the modules below import as bare names.
import jira_hierarchy_config
import pytest
from jira_hierarchy_config import (
    VALID_HIERARCHY_FIELDS,
    JiraHierarchyConfig,
    JiraHierarchyConfigError,
    JiraHierarchyConfigManager,
    JiraHierarchyUnmappedError,
    get_hierarchy_config,
    reset_for_tests,
    resolve_hierarchy_field,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """Return a tmp path for the hierarchy YAML (not yet created)."""
    return tmp_path / "jira-hierarchy.yaml"


@pytest.fixture(autouse=True)
def _isolate_singleton(tmp_path: Path):
    """Replace the module-level _MANAGER with one pointing at an absent file.

    Prevents tests from reading any real ``~/.config/egg/jira-hierarchy.yaml``
    and keeps state from leaking between tests.
    """
    reset_for_tests(config_path=tmp_path / "nonexistent.yaml")
    yield
    reset_for_tests(config_path=tmp_path / "nonexistent.yaml")


def _write(path: Path, body: str) -> None:
    path.write_text(body)


def _bump_mtime(path: Path, *, delta: float = 60.0) -> None:
    """Force ``path``'s mtime forward so coarse FS timestamps still see a change."""
    future = time.time() + delta
    os.utime(path, (future, future))


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestModuleSurface:
    def test_valid_hierarchy_fields_locked_down(self) -> None:
        assert VALID_HIERARCHY_FIELDS == frozenset({"parent", "epic_link"})

    def test_config_dataclass_is_frozen(self) -> None:
        cfg = JiraHierarchyConfig(projects={"ENG": "parent"})
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.projects = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Happy-path loading
# ---------------------------------------------------------------------------


class TestLoaderHappyPath:
    def test_loads_parent_mapping(self, config_path: Path) -> None:
        _write(
            config_path,
            "projects:\n  ENG: parent\n",
        )
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        cfg = mgr.get_config()
        assert cfg.projects == {"ENG": "parent"}
        assert cfg.field_for("ENG") == "parent"

    def test_loads_epic_link_mapping(self, config_path: Path) -> None:
        _write(
            config_path,
            "projects:\n  KORE: epic_link\n",
        )
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        assert mgr.get_config().field_for("KORE") == "epic_link"

    def test_loads_multiple_projects(self, config_path: Path) -> None:
        _write(
            config_path,
            "projects:\n  ENG: parent\n  KORE: epic_link\n  OPS: parent\n",
        )
        cfg = JiraHierarchyConfigManager(config_path=config_path).get_config()
        assert cfg.projects == {
            "ENG": "parent",
            "KORE": "epic_link",
            "OPS": "parent",
        }

    def test_comments_in_yaml_ok(self, config_path: Path) -> None:
        _write(
            config_path,
            "# top-level comment\nprojects:\n  # ENG team\n  ENG: parent\n",
        )
        cfg = JiraHierarchyConfigManager(config_path=config_path).get_config()
        assert cfg.projects == {"ENG": "parent"}


# ---------------------------------------------------------------------------
# Empty / missing file
# ---------------------------------------------------------------------------


class TestMissingOrEmpty:
    def test_missing_file_yields_empty_config(self, config_path: Path) -> None:
        # config_path is intentionally not created.
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        cfg = mgr.get_config()
        assert isinstance(cfg, JiraHierarchyConfig)
        assert cfg.projects == {}

    def test_empty_file_yields_empty_config(self, config_path: Path) -> None:
        _write(config_path, "")
        cfg = JiraHierarchyConfigManager(config_path=config_path).get_config()
        assert cfg.projects == {}

    def test_only_comments_yields_empty_config(self, config_path: Path) -> None:
        _write(config_path, "# just comments\n# nothing else\n")
        cfg = JiraHierarchyConfigManager(config_path=config_path).get_config()
        assert cfg.projects == {}

    def test_projects_null_yields_empty_config(self, config_path: Path) -> None:
        # ``projects: null`` ≡ key present with None value — module treats
        # that as "no projects" rather than an error.
        _write(config_path, "projects: null\n")
        cfg = JiraHierarchyConfigManager(config_path=config_path).get_config()
        assert cfg.projects == {}


# ---------------------------------------------------------------------------
# Malformed / adversarial input → JiraHierarchyConfigError
# ---------------------------------------------------------------------------


class TestValidationErrors:
    def test_malformed_yaml_raises(self, config_path: Path) -> None:
        _write(config_path, "projects: [unterminated\n")
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        with pytest.raises(JiraHierarchyConfigError) as excinfo:
            mgr.get_config()
        assert "not valid YAML" in str(excinfo.value)

    def test_top_level_not_mapping_raises(self, config_path: Path) -> None:
        # A top-level YAML list rather than a mapping.
        _write(config_path, "- one\n- two\n")
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        with pytest.raises(JiraHierarchyConfigError) as excinfo:
            mgr.get_config()
        assert "must be a YAML mapping" in str(excinfo.value)

    def test_projects_not_mapping_raises(self, config_path: Path) -> None:
        _write(config_path, "projects:\n  - ENG\n  - KORE\n")
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        with pytest.raises(JiraHierarchyConfigError) as excinfo:
            mgr.get_config()
        assert "'projects:' must be a mapping" in str(excinfo.value)

    def test_unknown_hierarchy_value_raises(self, config_path: Path) -> None:
        _write(config_path, "projects:\n  ENG: nope\n")
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        with pytest.raises(JiraHierarchyConfigError) as excinfo:
            mgr.get_config()
        assert "value must be one of" in str(excinfo.value)
        assert "'nope'" in str(excinfo.value)

    def test_non_string_value_raises(self, config_path: Path) -> None:
        _write(config_path, "projects:\n  ENG: 42\n")
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        with pytest.raises(JiraHierarchyConfigError):
            mgr.get_config()

    def test_empty_project_key_raises(self, config_path: Path) -> None:
        # YAML maps an empty key — the validator rejects empty string keys.
        _write(config_path, 'projects:\n  "": parent\n')
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        with pytest.raises(JiraHierarchyConfigError) as excinfo:
            mgr.get_config()
        assert "non-empty string" in str(excinfo.value)

    def test_non_string_project_key_raises(self, config_path: Path) -> None:
        # An integer key is rejected (non-str).
        _write(config_path, "projects:\n  1: parent\n")
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        with pytest.raises(JiraHierarchyConfigError) as excinfo:
            mgr.get_config()
        assert "non-empty string" in str(excinfo.value)


# ---------------------------------------------------------------------------
# mtime caching
# ---------------------------------------------------------------------------


class TestMtimeCaching:
    def test_same_mtime_skips_reload(self, config_path: Path) -> None:
        _write(config_path, "projects:\n  ENG: parent\n")
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        first = mgr.get_config()
        second = mgr.get_config()
        # Without an mtime bump the manager returns the cached instance.
        assert first is second

    def test_changed_mtime_triggers_reload(self, config_path: Path) -> None:
        _write(config_path, "projects:\n  ENG: parent\n")
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        first = mgr.get_config()
        assert first.projects == {"ENG": "parent"}

        _write(config_path, "projects:\n  ENG: epic_link\n  KORE: parent\n")
        _bump_mtime(config_path)

        second = mgr.get_config()
        assert second is not first
        assert second.projects == {"ENG": "epic_link", "KORE": "parent"}

    def test_missing_then_created_promotes_to_parsed(self, config_path: Path) -> None:
        # First call on a missing file: empty mapping cached.
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        first = mgr.get_config()
        assert first.projects == {}

        # Author the file; subsequent get_config() must reload, NOT keep
        # the empty mapping.
        _write(config_path, "projects:\n  ENG: parent\n")
        second = mgr.get_config()
        assert second.projects == {"ENG": "parent"}

    def test_present_then_missing_falls_back_to_empty(self, config_path: Path) -> None:
        _write(config_path, "projects:\n  ENG: parent\n")
        mgr = JiraHierarchyConfigManager(config_path=config_path)
        assert mgr.get_config().projects == {"ENG": "parent"}

        config_path.unlink()
        assert mgr.get_config().projects == {}


# ---------------------------------------------------------------------------
# Thread safety smoke test
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_get_config(self, config_path: Path) -> None:
        _write(config_path, "projects:\n  ENG: parent\n  KORE: epic_link\n")
        mgr = JiraHierarchyConfigManager(config_path=config_path)

        results: list[JiraHierarchyConfig] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append(mgr.get_config())
            except BaseException as exc:  # noqa: BLE001 — propagate after join
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"thread errors: {errors!r}"
        assert len(results) == 16
        for cfg in results:
            assert cfg.projects == {"ENG": "parent", "KORE": "epic_link"}


# ---------------------------------------------------------------------------
# Unmapped projects / resolve_hierarchy_field
# ---------------------------------------------------------------------------


class TestUnmappedProjects:
    def test_field_for_unknown_raises(self, config_path: Path) -> None:
        _write(config_path, "projects:\n  ENG: parent\n")
        reset_for_tests(config_path=config_path)
        cfg = get_hierarchy_config()
        with pytest.raises(JiraHierarchyUnmappedError) as excinfo:
            cfg.field_for("MISSING")
        err = excinfo.value
        assert err.project_key == "MISSING"
        # The error references the active manager's config path.
        assert err.config_path == config_path
        msg = str(err)
        assert "MISSING" in msg
        assert str(config_path) in msg
        assert "'parent' or 'epic_link'" in msg

    def test_resolve_hierarchy_field_unmapped(self, config_path: Path) -> None:
        _write(config_path, "projects:\n  ENG: parent\n")
        reset_for_tests(config_path=config_path)
        # Sanity: mapped key works.
        assert resolve_hierarchy_field("ENG") == "parent"
        # Unmapped key raises.
        with pytest.raises(JiraHierarchyUnmappedError):
            resolve_hierarchy_field("KORE")

    def test_resolve_hierarchy_field_missing_config(self, tmp_path: Path) -> None:
        # No config file at all — every resolve fails with unmapped error
        # (not a config error), so the caller's HITL gate fires per project.
        reset_for_tests(config_path=tmp_path / "absent.yaml")
        with pytest.raises(JiraHierarchyUnmappedError):
            resolve_hierarchy_field("ANY")


# ---------------------------------------------------------------------------
# Singleton glue
# ---------------------------------------------------------------------------


class TestSingletonGlue:
    def test_reset_for_tests_swaps_manager(self, config_path: Path) -> None:
        _write(config_path, "projects:\n  ENG: parent\n")
        mgr = reset_for_tests(config_path=config_path)
        assert isinstance(mgr, JiraHierarchyConfigManager)
        # The module-level _MANAGER now points at the new manager.
        assert jira_hierarchy_config._MANAGER is mgr  # type: ignore[attr-defined]
        assert get_hierarchy_config().projects == {"ENG": "parent"}

    def test_get_hierarchy_config_returns_singleton_view(self, config_path: Path) -> None:
        _write(config_path, "projects:\n  ENG: parent\n")
        reset_for_tests(config_path=config_path)
        a = get_hierarchy_config()
        b = get_hierarchy_config()
        # Backed by the same manager → identical cached object until mtime
        # changes.
        assert a is b
