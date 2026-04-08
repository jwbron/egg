"""
Verification tests for DevserverManager removal (issue #1558).

Confirms that all DevserverManager code, Docker Compose deployment
validation, and related imports have been fully removed without
leaving dangling references.
"""

import ast
import os
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def _python_files(directory: Path):
    """Yield all .py files under directory, skipping __pycache__ and .egg-state."""
    for root, dirs, files in os.walk(directory):
        # Skip non-source directories
        dirs[:] = [
            d
            for d in dirs
            if d not in ("__pycache__", ".egg-state", ".git", ".venv", "node_modules")
        ]
        for f in files:
            if f.endswith(".py"):
                yield Path(root) / f


class TestDevserverModuleRemoval:
    """Verify deleted modules no longer exist."""

    def test_devserver_module_deleted(self):
        """orchestrator/devserver.py must not exist."""
        assert not (REPO_ROOT / "orchestrator" / "devserver.py").exists()

    def test_checks_route_deleted(self):
        """orchestrator/routes/checks.py must not exist."""
        assert not (REPO_ROOT / "orchestrator" / "routes" / "checks.py").exists()

    def test_deployment_module_deleted(self):
        """shared/egg_contracts/deployment.py must not exist."""
        assert not (REPO_ROOT / "shared" / "egg_contracts" / "deployment.py").exists()

    def test_devserver_unit_tests_deleted(self):
        """orchestrator/tests/test_devserver.py must not exist."""
        assert not (REPO_ROOT / "orchestrator" / "tests" / "test_devserver.py").exists()

    def test_routes_checks_tests_deleted(self):
        """orchestrator/tests/test_routes_checks.py must not exist."""
        assert not (REPO_ROOT / "orchestrator" / "tests" / "test_routes_checks.py").exists()

    def test_deployment_config_tests_deleted(self):
        """tests/shared/egg_contracts/test_deployment_config.py must not exist."""
        assert not (
            REPO_ROOT / "tests" / "shared" / "egg_contracts" / "test_deployment_config.py"
        ).exists()

    def test_deployment_validation_e2e_deleted(self):
        """integration_tests/deployment_validation/ directory must not exist."""
        assert not (REPO_ROOT / "integration_tests" / "deployment_validation").exists()


class TestNoDanglingImports:
    """Verify no Python source files import removed modules."""

    # Symbols that should no longer appear as imports in any source file
    REMOVED_SYMBOLS = {
        "DevserverManager",
        "DevserverError",
        "ComposeExtractionError",
        "NetworkError",
        "StackLifecycleError",
        "DevserverStatusValue",
        "DevserverStatus",
        "ServiceStatus",
        "DeploymentConfig",
        "ServiceMapping",
        "ValidationTest",
        "load_deployment_config",
        "check_suspicious_env_vars",
        "teardown_devserver",
        "checks_bp",
    }

    REMOVED_MODULES = {
        "devserver",
        "routes.checks",
        "egg_contracts.deployment",
    }

    def _get_source_dirs(self):
        """Return source directories to scan (skip test dirs for import check)."""
        return [
            REPO_ROOT / "orchestrator",
            REPO_ROOT / "shared",
            REPO_ROOT / "gateway",
        ]

    def test_no_imports_of_removed_modules(self):
        """No source file should import from removed modules."""
        violations = []
        for source_dir in self._get_source_dirs():
            if not source_dir.exists():
                continue
            for py_file in _python_files(source_dir):
                # Skip test files — they're checked separately
                if "tests" in py_file.parts or "test_" in py_file.name:
                    continue
                try:
                    tree = ast.parse(py_file.read_text(encoding="utf-8"))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            for removed in self.REMOVED_MODULES:
                                if alias.name == removed or alias.name.endswith(f".{removed}"):
                                    violations.append(
                                        f"{py_file.relative_to(REPO_ROOT)}:{node.lineno}: "
                                        f"import {alias.name}"
                                    )
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            for removed in self.REMOVED_MODULES:
                                if node.module == removed or node.module.endswith(f".{removed}"):
                                    names = ", ".join(a.name for a in node.names)
                                    violations.append(
                                        f"{py_file.relative_to(REPO_ROOT)}:{node.lineno}: "
                                        f"from {node.module} import {names}"
                                    )
        assert violations == [], "Found imports of removed modules:\n" + "\n".join(
            f"  {v}" for v in violations
        )

    def test_no_removed_symbols_in_source_imports(self):
        """No source file should import removed symbols."""
        violations = []
        for source_dir in self._get_source_dirs():
            if not source_dir.exists():
                continue
            for py_file in _python_files(source_dir):
                if "tests" in py_file.parts or "test_" in py_file.name:
                    continue
                try:
                    tree = ast.parse(py_file.read_text(encoding="utf-8"))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.names:
                        for alias in node.names:
                            if alias.name in self.REMOVED_SYMBOLS:
                                violations.append(
                                    f"{py_file.relative_to(REPO_ROOT)}:{node.lineno}: "
                                    f"from {node.module} import {alias.name}"
                                )
        assert violations == [], "Found imports of removed symbols:\n" + "\n".join(
            f"  {v}" for v in violations
        )


class TestConstantsCleanup:
    """Verify DEVSERVER_* and EGG_CHECK_NETWORK_PREFIX constants removed."""

    def test_no_devserver_constants(self):
        """constants.py must not contain DEVSERVER_* constants."""
        constants_path = REPO_ROOT / "shared" / "egg_config" / "constants.py"
        content = constants_path.read_text(encoding="utf-8")
        removed_constants = [
            "DEVSERVER_CPU_LIMIT",
            "DEVSERVER_MEMORY_LIMIT",
            "DEVSERVER_PIDS_LIMIT",
            "DEVSERVER_HARD_TIMEOUT_SECONDS",
            "EGG_CHECK_NETWORK_PREFIX",
        ]
        found = [c for c in removed_constants if c in content]
        assert found == [], f"Constants still present in constants.py: {found}"

    def test_no_devserver_in_all_list(self):
        """__all__ in constants.py must not reference removed constants."""
        constants_path = REPO_ROOT / "shared" / "egg_config" / "constants.py"
        tree = ast.parse(constants_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        if isinstance(node.value, ast.List):
                            exported = [
                                elt.value
                                for elt in node.value.elts
                                if isinstance(elt, ast.Constant)
                            ]
                            devserver_exports = [
                                e for e in exported if "DEVSERVER" in e or "EGG_CHECK_NETWORK" in e
                            ]
                            assert devserver_exports == [], (
                                f"Removed constants in __all__: {devserver_exports}"
                            )


class TestContractsInitCleanup:
    """Verify egg_contracts/__init__.py no longer exports deployment symbols."""

    def test_no_deployment_exports(self):
        """egg_contracts/__init__.py must not import from .deployment."""
        init_path = REPO_ROOT / "shared" / "egg_contracts" / "__init__.py"
        content = init_path.read_text(encoding="utf-8")
        assert "deployment" not in content.lower(), (
            "egg_contracts/__init__.py still references 'deployment'"
        )

    def test_no_deployment_symbols_in_all(self):
        """__all__ in egg_contracts/__init__.py must not contain deployment symbols."""
        init_path = REPO_ROOT / "shared" / "egg_contracts" / "__init__.py"
        tree = ast.parse(init_path.read_text(encoding="utf-8"))
        deployment_symbols = {
            "DeploymentConfig",
            "ServiceMapping",
            "ValidationTest",
            "load_deployment_config",
            "check_suspicious_env_vars",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        if isinstance(node.value, ast.List):
                            exported = {
                                elt.value
                                for elt in node.value.elts
                                if isinstance(elt, ast.Constant)
                            }
                            found = exported & deployment_symbols
                            assert found == set(), f"Deployment symbols still in __all__: {found}"


class TestApiCleanup:
    """Verify orchestrator/api.py no longer references checks blueprint."""

    def test_no_checks_bp_import(self):
        """api.py must not import checks_bp."""
        api_path = REPO_ROOT / "orchestrator" / "api.py"
        content = api_path.read_text(encoding="utf-8")
        assert "checks_bp" not in content, "api.py still references checks_bp"
        assert "routes.checks" not in content, "api.py still references routes.checks"

    def test_no_deployment_check_routes(self):
        """api.py must not register deployment check routes."""
        api_path = REPO_ROOT / "orchestrator" / "api.py"
        content = api_path.read_text(encoding="utf-8")
        assert "deployment-check" not in content
        assert "deployment_check" not in content


class TestPhasesCleanup:
    """Verify orchestrator/routes/phases.py no longer references devserver teardown."""

    def test_no_teardown_devserver_import(self):
        """phases.py must not import teardown_devserver."""
        phases_path = REPO_ROOT / "orchestrator" / "routes" / "phases.py"
        content = phases_path.read_text(encoding="utf-8")
        assert "teardown_devserver" not in content, "phases.py still references teardown_devserver"

    def test_no_devserver_references(self):
        """phases.py must not reference devserver at all."""
        phases_path = REPO_ROOT / "orchestrator" / "routes" / "phases.py"
        tree = ast.parse(phases_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "checks" not in node.module or "health_checks" in node.module, (
                    f"phases.py imports from {node.module} — expected routes.checks removed"
                )
