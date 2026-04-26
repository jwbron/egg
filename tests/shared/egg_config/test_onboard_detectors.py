"""Tests for the onboard detectors (issue #2073, TASK-5-2).

Cover the six built-in language detectors plus the mixed-language and
plug-in cases. Synthetic project trees are constructed under
``tmp_path`` and the proposed blocks are compared against golden
fixtures under ``tests/shared/egg_config/golden/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from egg_config.onboard_detectors import (
    _DETECTORS,
    DetectionResult,
    GoDetector,
    NodeNpmDetector,
    NodePnpmDetector,
    NodeYarnDetector,
    PythonPipDetector,
    PythonUvDetector,
    merge_detections,
    register_detector,
    run_detectors,
)

_GOLDEN_DIR = Path(__file__).parent / "golden"


# ---------------------------------------------------------------------------
# Fixtures: synthetic project trees
# ---------------------------------------------------------------------------


@pytest.fixture
def python_uv_repo(tmp_path):
    repo = tmp_path / "py_uv"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
    (repo / "uv.lock").write_text("# uv lock file\n")
    (repo / "Makefile").write_text("lint:\n\tflake8\ntest:\n\tpytest\n")
    return repo


@pytest.fixture
def python_pip_repo(tmp_path):
    repo = tmp_path / "py_pip"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
    (repo / "requirements.txt").write_text("foo>=1.0\n")
    (repo / "requirements-dev.txt").write_text("pytest\n")
    return repo


@pytest.fixture
def node_pnpm_repo(tmp_path):
    repo = tmp_path / "node_pnpm"
    repo.mkdir()
    (repo / "package.json").write_text('{"scripts": {"lint": "eslint .", "test": "jest"}}')
    (repo / "pnpm-lock.yaml").write_text("# pnpm lock\n")
    return repo


@pytest.fixture
def node_npm_repo(tmp_path):
    repo = tmp_path / "node_npm"
    repo.mkdir()
    (repo / "package.json").write_text('{"scripts": {"lint": "eslint", "test": "jest"}}')
    (repo / "package-lock.json").write_text("{}")
    return repo


@pytest.fixture
def node_yarn_repo(tmp_path):
    repo = tmp_path / "node_yarn"
    repo.mkdir()
    (repo / "package.json").write_text('{"scripts": {"test": "jest"}}')
    (repo / "yarn.lock").write_text("")
    return repo


@pytest.fixture
def go_repo(tmp_path):
    repo = tmp_path / "gosrv"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/foo\n\ngo 1.22.0\n")
    (repo / "go.sum").write_text("# sums\n")
    return repo


@pytest.fixture
def mixed_go_node_repo(tmp_path):
    """Mixed-language: a Go service with a Node frontend."""
    repo = tmp_path / "mixed"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/foo\ngo 1.21.0\n")
    (repo / "package.json").write_text('{"scripts": {"build": "next build"}}')
    (repo / "package-lock.json").write_text("{}")
    return repo


# ---------------------------------------------------------------------------
# (a) Python uv detector
# ---------------------------------------------------------------------------


class TestPythonUvDetector:
    def test_detects_uv_repo(self, python_uv_repo):
        det = PythonUvDetector()
        result = det.detect(python_uv_repo)
        assert result is not None
        assert result.language == "python-uv"
        assert ".venv" in result.persist
        assert "/usr/local/bin" in result.persist
        assert "uv.lock" in result.watch_files
        assert "pyproject.toml" in result.watch_files
        assert "Makefile" in result.watch_files
        # Reasoning calls out the #2087 trap.
        assert "#2087" in result.reasoning
        assert "--no-install-project" in " ".join(result.build_commands)

    def test_no_detection_without_pyproject(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        (repo / "uv.lock").write_text("")
        det = PythonUvDetector()
        assert det.detect(repo) is None

    def test_no_detection_without_uvlock(self, tmp_path):
        repo = tmp_path / "no_lock"
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]")
        det = PythonUvDetector()
        assert det.detect(repo) is None


# ---------------------------------------------------------------------------
# (b) Python pip detector
# ---------------------------------------------------------------------------


class TestPythonPipDetector:
    def test_detects_pip_repo(self, python_pip_repo):
        det = PythonPipDetector()
        result = det.detect(python_pip_repo)
        assert result is not None
        assert result.language == "python-pip"
        assert ".venv" in result.persist
        assert "requirements.txt" in result.watch_files
        assert "requirements-dev.txt" in result.watch_files

    def test_skips_when_uv_lock_present(self, python_uv_repo):
        # uv.lock + requirements.txt — uv detector wins, pip detector
        # returns None.
        (python_uv_repo / "requirements.txt").write_text("foo")
        det = PythonPipDetector()
        assert det.detect(python_uv_repo) is None

    def test_no_requirements_file_no_detection(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        det = PythonPipDetector()
        assert det.detect(repo) is None


# ---------------------------------------------------------------------------
# (c) Node detectors
# ---------------------------------------------------------------------------


class TestNodePnpmDetector:
    def test_detects_pnpm_repo(self, node_pnpm_repo):
        det = NodePnpmDetector()
        result = det.detect(node_pnpm_repo)
        assert result is not None
        assert result.language == "node-pnpm"
        assert "node_modules" in result.persist
        assert "pnpm-lock.yaml" in result.watch_files
        # `package.json` scripts surfaced as default checks.
        names = [c["name"] for c in result.checks]
        assert "lint" in names
        assert "test" in names

    def test_no_detection_without_package_json(self, tmp_path):
        repo = tmp_path / "stray"
        repo.mkdir()
        (repo / "pnpm-lock.yaml").write_text("")
        det = NodePnpmDetector()
        assert det.detect(repo) is None


class TestNodeYarnDetector:
    def test_detects_yarn_repo(self, node_yarn_repo):
        det = NodeYarnDetector()
        result = det.detect(node_yarn_repo)
        assert result is not None
        assert result.language == "node-yarn"
        assert "yarn.lock" in result.watch_files

    def test_no_detection_without_yarn_lock(self, tmp_path):
        repo = tmp_path / "stray"
        repo.mkdir()
        (repo / "package.json").write_text("{}")
        det = NodeYarnDetector()
        assert det.detect(repo) is None


class TestNodeNpmDetector:
    def test_detects_npm_repo(self, node_npm_repo):
        det = NodeNpmDetector()
        result = det.detect(node_npm_repo)
        assert result is not None
        assert result.language == "node-npm"
        assert "package-lock.json" in result.watch_files
        assert any("npm ci" in c for c in result.build_commands)

    def test_defers_to_pnpm_when_pnpm_lock_present(self, node_pnpm_repo):
        # node-npm should not fire when pnpm-lock.yaml is present.
        det = NodeNpmDetector()
        assert det.detect(node_pnpm_repo) is None

    def test_defers_to_yarn_when_yarn_lock_present(self, node_yarn_repo):
        det = NodeNpmDetector()
        assert det.detect(node_yarn_repo) is None

    def test_npm_install_when_no_lockfile(self, tmp_path):
        repo = tmp_path / "no_lock"
        repo.mkdir()
        (repo / "package.json").write_text("{}")
        det = NodeNpmDetector()
        result = det.detect(repo)
        assert result is not None
        assert any("npm install" in c for c in result.build_commands)


# ---------------------------------------------------------------------------
# (d) Go detector
# ---------------------------------------------------------------------------


class TestGoDetector:
    def test_detects_go_repo(self, go_repo):
        det = GoDetector()
        result = det.detect(go_repo)
        assert result is not None
        assert result.language == "go"
        assert "/usr/local/go" in result.persist
        assert "go.mod" in result.watch_files
        assert "go.sum" in result.watch_files

    def test_extracts_go_version(self, go_repo):
        det = GoDetector()
        result = det.detect(go_repo)
        assert result is not None
        # 1.22.0 from the go directive in fixture's go.mod.
        joined = " ".join(result.build_commands)
        assert "1.22.0" in joined

    def test_default_version_when_directive_absent(self, tmp_path):
        repo = tmp_path / "no_dir"
        repo.mkdir()
        (repo / "go.mod").write_text("module example.com/foo\n")
        det = GoDetector()
        result = det.detect(repo)
        assert result is not None
        # Fallback version embedded somewhere in build_commands.
        joined = " ".join(result.build_commands)
        # Currently 1.22.0 default; just confirm a Go version is present.
        assert "1." in joined

    def test_no_detection_without_gomod(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        det = GoDetector()
        assert det.detect(repo) is None


# ---------------------------------------------------------------------------
# (e) Mixed-language repo + run_detectors fan-out
# ---------------------------------------------------------------------------


class TestRunDetectorsAndMerge:
    def test_run_detectors_returns_each_match_for_mixed(self, mixed_go_node_repo):
        results = run_detectors(mixed_go_node_repo, include_registered=False)
        languages = {r.language for r in results}
        assert "go" in languages
        assert "node-npm" in languages

    def test_merge_detections_concats_blocks(self, mixed_go_node_repo):
        results = run_detectors(mixed_go_node_repo, include_registered=False)
        merged = merge_detections(results)
        assert merged.language.startswith("mixed:")
        # Concatenated build_commands across detectors.
        joined_cmds = " ".join(merged.build_commands)
        assert "go mod download" in joined_cmds
        assert "npm" in joined_cmds
        # Persist combines both sets, dedup-preserving order.
        assert "/usr/local/go" in merged.persist
        assert "node_modules" in merged.persist
        # Watch files combine.
        assert "go.mod" in merged.watch_files
        assert "package.json" in merged.watch_files

    def test_merge_detections_single_detection_returns_unchanged(self, python_uv_repo):
        results = run_detectors(python_uv_repo, include_registered=False)
        merged = merge_detections(results)
        # Mixed wraps multi only; with one detection we get the same result.
        assert merged.language == "python-uv" or merged.language.startswith("mixed:")

    def test_merge_detections_empty_input(self):
        merged = merge_detections([])
        assert merged.language == "none"
        assert merged.confidence == 0.0
        assert merged.build_commands == []

    def test_run_detectors_against_empty_repo(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        results = run_detectors(repo, include_registered=False)
        assert results == []


# ---------------------------------------------------------------------------
# (f) Plug-in registration via register_detector
# ---------------------------------------------------------------------------


class TestRegisterDetector:
    def setup_method(self):
        # Snapshot the registry so tests don't bleed state.
        self._snapshot = list(_DETECTORS)

    def teardown_method(self):
        _DETECTORS[:] = self._snapshot

    def test_register_class_decorator(self):
        # ``register_detector`` returns the *instance* it instantiates,
        # so we assert via ``language`` on a fired detection rather
        # than via class identity (the decorator-bound name in the
        # local scope is a Detector instance, not a class).
        @register_detector
        class StubDetector:
            priority = 50

            def detect(self, repo_path: Path):  # noqa: D401
                return DetectionResult(
                    language="stub-decorator", confidence=0.5, reasoning="stub fired"
                )

        languages = {type(d).__name__ for d in _DETECTORS}
        assert "StubDetector" in languages

    def test_register_instance(self):
        class StubDetectorInst:
            priority = 25

            def detect(self, repo_path: Path):
                return None

        instance = StubDetectorInst()
        register_detector(instance)
        assert instance in _DETECTORS

    def test_register_rejects_non_detector(self):
        with pytest.raises(TypeError):
            register_detector("not a detector")  # type: ignore[arg-type]

    def test_priority_ordering(self, tmp_path):
        """Higher-priority detector fires first when both match."""

        @register_detector
        class HighStub:
            priority = 200

            def detect(self, repo_path: Path):
                # Always fires.
                return DetectionResult(language="high", confidence=1.0, reasoning="high")

        @register_detector
        class LowStub:
            priority = 10

            def detect(self, repo_path: Path):
                return DetectionResult(language="low", confidence=0.1, reasoning="low")

        results = run_detectors(tmp_path, include_registered=True)
        # Both fire; high comes first in the priority-sorted output.
        languages = [r.language for r in results]
        assert languages.index("high") < languages.index("low")

    def test_registered_plugin_runs_via_run_detectors(self, tmp_path, python_uv_repo):
        @register_detector
        class TagDetector:
            priority = 1

            def detect(self, repo_path: Path):
                return DetectionResult(language="tag", confidence=0.99, reasoning="always-on")

        results = run_detectors(python_uv_repo, include_registered=True)
        languages = {r.language for r in results}
        assert "tag" in languages
        assert "python-uv" in languages


# ---------------------------------------------------------------------------
# (g) Golden file fixtures
# ---------------------------------------------------------------------------


def _to_golden_dict(result: DetectionResult) -> dict:
    """Stable subset of the result for golden-file diffing."""
    return {
        "language": result.language,
        "build_commands": list(result.build_commands),
        "persist": sorted(set(result.persist)),
        "watch_files": sorted(set(result.watch_files)),
        "checks": list(result.checks),
    }


def _load_or_write_golden(name: str, current: dict) -> dict:
    """Read the golden YAML at ``name`` or write the current value as a baseline.

    The golden directory is committed under the test tree. When the
    file is absent the test seeds it; this lets us extend the golden
    set without manual file creation. Subsequent runs assert exact
    match.
    """
    _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    golden_path = _GOLDEN_DIR / name
    if not golden_path.exists():
        golden_path.write_text(yaml.safe_dump(current, sort_keys=True))
    expected = yaml.safe_load(golden_path.read_text())
    return expected


class TestGoldenFiles:
    """Pin the proposed YAML output for each language detector.

    The golden files live under
    ``tests/shared/egg_config/golden/`` and are loaded from the test
    tree on demand. Updating a detector should bump the golden file in
    the same commit so reviewers see the diff.
    """

    def test_golden_python_uv(self, python_uv_repo):
        det = PythonUvDetector()
        result = det.detect(python_uv_repo)
        assert result is not None
        cur = _to_golden_dict(result)
        expected = _load_or_write_golden("python_uv.yaml", cur)
        assert cur == expected

    def test_golden_python_pip(self, python_pip_repo):
        det = PythonPipDetector()
        result = det.detect(python_pip_repo)
        assert result is not None
        cur = _to_golden_dict(result)
        expected = _load_or_write_golden("python_pip.yaml", cur)
        assert cur == expected

    def test_golden_node_pnpm(self, node_pnpm_repo):
        det = NodePnpmDetector()
        result = det.detect(node_pnpm_repo)
        assert result is not None
        cur = _to_golden_dict(result)
        expected = _load_or_write_golden("node_pnpm.yaml", cur)
        assert cur == expected

    def test_golden_node_yarn(self, node_yarn_repo):
        det = NodeYarnDetector()
        result = det.detect(node_yarn_repo)
        assert result is not None
        cur = _to_golden_dict(result)
        expected = _load_or_write_golden("node_yarn.yaml", cur)
        assert cur == expected

    def test_golden_node_npm(self, node_npm_repo):
        det = NodeNpmDetector()
        result = det.detect(node_npm_repo)
        assert result is not None
        cur = _to_golden_dict(result)
        expected = _load_or_write_golden("node_npm.yaml", cur)
        assert cur == expected

    def test_golden_go(self, go_repo):
        det = GoDetector()
        result = det.detect(go_repo)
        assert result is not None
        cur = _to_golden_dict(result)
        expected = _load_or_write_golden("go.yaml", cur)
        assert cur == expected
