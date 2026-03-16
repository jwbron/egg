"""Validation tests for issue #1165: removal of per-phase dispatch paths.

These tests verify that the following concepts have been fully removed:
- Tier 3 phase-level dispatch
- Multi-agent wave execution
- Single-agent fallback
- Short-circuit mode
- Complexity tiers
- Integrator role
- MultiAgentConfig

And that the surviving code path (concurrent BRC) works correctly as the
only execution mode.
"""

import ast
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add orchestrator and shared to path (consistent with conftest.py)
_project_root = Path(__file__).parent.parent.parent
_orchestrator_path = _project_root / "orchestrator"
_shared_path = _project_root / "shared"

for p in (_orchestrator_path, _shared_path):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

from models import Pipeline, PipelineConfig, PipelinePhase, PipelineStatus


def _make_pipeline(pipeline_id: str = "issue-test") -> Pipeline:
    """Create a test pipeline."""
    config = PipelineConfig()
    return Pipeline(
        id=pipeline_id,
        repo="test/repo",
        issue_number=42,
        status=PipelineStatus.RUNNING,
        current_phase=PipelinePhase.IMPLEMENT,
        config=config,
    )


# ---------------------------------------------------------------------------
# Section 1: Deleted files must not exist
# ---------------------------------------------------------------------------


class TestDeletedFiles:
    """Verify that files scheduled for deletion are actually gone."""

    @pytest.mark.parametrize(
        "relative_path",
        [
            "orchestrator/multi_agent.py",
            "orchestrator/dispatch.py",
        ],
    )
    def test_deleted_source_files(self, relative_path: str):
        path = _project_root / relative_path
        assert not path.exists(), f"{relative_path} should have been deleted"

    @pytest.mark.parametrize(
        "relative_path",
        [
            "orchestrator/tests/test_tier3_execute.py",
            "orchestrator/tests/test_tier3_dispatch.py",
            "orchestrator/tests/test_multi_agent.py",
            "orchestrator/tests/test_dispatch.py",
            "orchestrator/tests/test_short_circuit.py",
            "shared/egg_contracts/tests/test_phase_dependency_graph.py",
            "shared/egg_contracts/tests/test_agent_roles_tier3.py",
            "gateway/tests/test_integrator_tier3.py",
        ],
    )
    def test_deleted_test_files(self, relative_path: str):
        path = _project_root / relative_path
        assert not path.exists(), f"{relative_path} should have been deleted"

    @pytest.mark.parametrize(
        "relative_path",
        [
            "sandbox/.claude/rules/integrator.md",
            "sandbox/.claude/commands/integrator-mode.md",
            "docs/reference/integrator-agent.md",
            "docs/guides/tier3-dispatch.md",
        ],
    )
    def test_deleted_doc_files(self, relative_path: str):
        path = _project_root / relative_path
        assert not path.exists(), f"{relative_path} should have been deleted"


# ---------------------------------------------------------------------------
# Section 2: is_concurrent_execution() lives only in concurrent_executor.py
# ---------------------------------------------------------------------------


class TestIsConcurrentExecutionRelocation:
    """Verify is_concurrent_execution() was relocated to concurrent_executor.py."""

    def test_importable_from_concurrent_executor(self):
        from concurrent_executor import is_concurrent_execution

        assert callable(is_concurrent_execution)

    def test_not_importable_from_multi_agent(self):
        """multi_agent.py should no longer exist."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("multi_agent")

    def test_returns_true_when_concurrent_execution_enabled(self):
        from concurrent_executor import is_concurrent_execution

        pipeline = _make_pipeline()
        pipeline.config.__dict__["concurrent_execution"] = True
        assert is_concurrent_execution(pipeline) is True

    def test_returns_true_for_phase_in_concurrent_phases(self):
        from concurrent_executor import is_concurrent_execution

        pipeline = _make_pipeline()
        pipeline.config.__dict__["concurrent_execution"] = False
        pipeline.config.__dict__["concurrent_phases"] = ["implement"]
        assert is_concurrent_execution(pipeline, phase="implement") is True

    def test_returns_false_for_phase_not_in_concurrent_phases(self):
        from concurrent_executor import is_concurrent_execution

        pipeline = _make_pipeline()
        pipeline.config.__dict__["concurrent_execution"] = False
        pipeline.config.__dict__["concurrent_phases"] = ["refine"]
        assert is_concurrent_execution(pipeline, phase="implement") is False


# ---------------------------------------------------------------------------
# Section 3: Integrator role removed from runtime
# ---------------------------------------------------------------------------


class TestIntegratorRoleRemoval:
    """Verify integrator is removed from phase role lists and dependencies."""

    def test_implement_phase_has_no_integrator(self):
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("implement", include_reviewers=False)
        role_values = [r.value for r in roles]
        assert "integrator" not in role_values

    def test_implement_phase_with_reviewers_has_no_integrator(self):
        from egg_contracts.agent_roles import get_roles_for_phase

        roles = get_roles_for_phase("implement", include_reviewers=True)
        role_values = [r.value for r in roles]
        assert "integrator" not in role_values

    def test_concurrent_executor_no_integrator_in_roles(self):
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        pipeline.current_phase = PipelinePhase.IMPLEMENT
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        roles = executor.get_agent_roles()
        role_values = [r.value for r in roles]
        assert "integrator" not in role_values

    def test_signals_role_map_no_integrator(self):
        """signals.py _AGENT_ROLE_TO_CONTRACT_ROLE should not map integrator."""
        from models import AgentRole
        from routes.signals import _AGENT_ROLE_TO_CONTRACT_ROLE

        # Check that INTEGRATOR is not in the mapping if it still exists as enum
        if hasattr(AgentRole, "INTEGRATOR"):
            assert AgentRole.INTEGRATOR not in _AGENT_ROLE_TO_CONTRACT_ROLE

    def test_reviewer_code_no_integrator_dependency(self):
        """REVIEWER_CODE_ROLE should not depend on integrator."""
        from egg_contracts.agent_roles import REVIEWER_CODE_ROLE

        dep_values = [d.value for d in REVIEWER_CODE_ROLE.dependencies]
        assert "integrator" not in dep_values

    def test_reviewer_contract_no_integrator_dependency(self):
        """REVIEWER_CONTRACT_ROLE should not depend on integrator."""
        from egg_contracts.agent_roles import REVIEWER_CONTRACT_ROLE

        dep_values = [d.value for d in REVIEWER_CONTRACT_ROLE.dependencies]
        assert "integrator" not in dep_values


# ---------------------------------------------------------------------------
# Section 4: ComplexityTier removed from models
# ---------------------------------------------------------------------------


class TestComplexityTierRemoval:
    """Verify ComplexityTier enum and related fields are removed."""

    def test_no_complexity_tier_enum(self):
        import models

        assert not hasattr(models, "ComplexityTier"), (
            "ComplexityTier enum should be removed from models"
        )

    def test_pipeline_no_complexity_tier_field(self):
        pipeline = _make_pipeline()
        # Field should not exist or should be removed
        schema = pipeline.model_json_schema()
        assert "complexity_tier" not in schema.get("properties", {}), (
            "Pipeline.complexity_tier field should be removed"
        )

    def test_pipeline_config_no_enable_parallel_phases(self):
        config = PipelineConfig()
        schema = config.model_json_schema()
        assert "enable_parallel_phases" not in schema.get("properties", {}), (
            "PipelineConfig.enable_parallel_phases should be removed"
        )


# ---------------------------------------------------------------------------
# Section 5: Short-circuit mode removed
# ---------------------------------------------------------------------------


class TestShortCircuitRemoval:
    """Verify short-circuit mode fields and logic are removed."""

    def test_pipeline_config_no_allow_short_circuit(self):
        config = PipelineConfig()
        schema = config.model_json_schema()
        assert "allow_short_circuit" not in schema.get("properties", {}), (
            "PipelineConfig.allow_short_circuit should be removed"
        )

    def test_pipeline_no_short_circuit_field(self):
        pipeline = _make_pipeline()
        schema = pipeline.model_json_schema()
        assert "short_circuit" not in schema.get("properties", {}), (
            "Pipeline.short_circuit field should be removed"
        )


# ---------------------------------------------------------------------------
# Section 6: MultiAgentConfig removed from contracts
# ---------------------------------------------------------------------------


class TestMultiAgentConfigRemoval:
    """Verify MultiAgentConfig is removed from egg_contracts."""

    def test_no_multi_agent_config_in_contracts(self):
        import egg_contracts.models as contract_models

        assert not hasattr(contract_models, "MultiAgentConfig"), (
            "MultiAgentConfig should be removed from egg_contracts.models"
        )

    def test_no_multi_agent_config_export(self):
        import egg_contracts

        assert not hasattr(egg_contracts, "MultiAgentConfig"), (
            "MultiAgentConfig should not be exported from egg_contracts"
        )


# ---------------------------------------------------------------------------
# Section 7: Pipeline config removed fields
# ---------------------------------------------------------------------------


class TestPipelineConfigCleanup:
    """Verify removed config fields."""

    def test_no_plan_phase_waves_field(self):
        pipeline = _make_pipeline()
        schema = pipeline.model_json_schema()
        assert "plan_phase_waves" not in schema.get("properties", {}), (
            "Pipeline.plan_phase_waves should be removed"
        )

    def test_no_plan_phase_names_field(self):
        pipeline = _make_pipeline()
        schema = pipeline.model_json_schema()
        assert "plan_phase_names" not in schema.get("properties", {}), (
            "Pipeline.plan_phase_names should be removed"
        )


# ---------------------------------------------------------------------------
# Section 8: dispatch.py no longer imported anywhere
# ---------------------------------------------------------------------------


class TestNoDispatchImports:
    """Verify no module imports dispatch.py."""

    def test_signals_no_dispatch_import(self):
        """signals.py should not import from dispatch module."""
        signals_path = _project_root / "orchestrator" / "routes" / "signals.py"
        if not signals_path.exists():
            pytest.skip("signals.py not found")
        source = signals_path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "dispatch" != node.module, (
                    f"signals.py still imports from dispatch at line {node.lineno}"
                )

    def test_pipelines_no_dispatch_import(self):
        """pipelines.py should not import from dispatch module."""
        pipelines_path = _project_root / "orchestrator" / "routes" / "pipelines.py"
        if not pipelines_path.exists():
            pytest.skip("pipelines.py not found")
        source = pipelines_path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "dispatch" != node.module, (
                    f"pipelines.py still imports from dispatch at line {node.lineno}"
                )


# ---------------------------------------------------------------------------
# Section 9: No multi_agent.py imports anywhere
# ---------------------------------------------------------------------------


class TestNoMultiAgentImports:
    """Verify no module imports from multi_agent.py."""

    def _check_no_multi_agent_import(self, filepath: Path):
        """Helper to check a file has no imports from multi_agent."""
        if not filepath.exists():
            return
        source = filepath.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "multi_agent" != node.module, (
                    f"{filepath.name} still imports from multi_agent at line {node.lineno}"
                )

    def test_pipelines_no_multi_agent_import(self):
        self._check_no_multi_agent_import(
            _project_root / "orchestrator" / "routes" / "pipelines.py"
        )

    def test_concurrent_executor_no_multi_agent_import(self):
        self._check_no_multi_agent_import(_project_root / "orchestrator" / "concurrent_executor.py")


# ---------------------------------------------------------------------------
# Section 10: Concurrent BRC is the only execution path
# ---------------------------------------------------------------------------


class TestConcurrentBRCOnlyPath:
    """Verify that the implement phase always uses concurrent BRC."""

    def test_executor_spawns_all_implement_roles(self):
        """ConcurrentPhaseExecutor should include standard implement roles."""
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        pipeline.current_phase = PipelinePhase.IMPLEMENT
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        roles = executor.get_agent_roles()
        role_values = {r.value for r in roles}

        # Core implement roles must be present
        assert "coder" in role_values
        assert "tester" in role_values
        assert "documenter" in role_values
        assert "reviewer_code" in role_values
        assert "reviewer_contract" in role_values

    def test_review_graph_roles_match_spawned_roles(self):
        """Review graph must be a subset of spawned roles."""
        from concurrent_executor import ConcurrentPhaseExecutor
        from review_graph import get_review_graph_for_phase

        pipeline = _make_pipeline()
        pipeline.current_phase = PipelinePhase.IMPLEMENT
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        roles = executor.get_agent_roles()
        role_names = {r.value for r in roles}
        graph = get_review_graph_for_phase("implement")
        graph_roles = graph.all_roles()

        assert graph_roles.issubset(role_names), (
            f"Review graph roles {graph_roles - role_names} not in spawned roles"
        )

    def test_brc_env_vars_set_for_all_roles(self):
        """All roles should get BRC env vars."""
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        pipeline.current_phase = PipelinePhase.IMPLEMENT
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        for role in executor.get_agent_roles():
            env = executor.get_agent_env(role)
            assert env.get("EGG_CONCURRENT_MODE") == "true", (
                f"Role {role.value} missing EGG_CONCURRENT_MODE"
            )
            assert "EGG_BRC_ROLE_TYPE" in env, f"Role {role.value} missing EGG_BRC_ROLE_TYPE"


# ---------------------------------------------------------------------------
# Section 11: No stale function references in pipelines.py
# ---------------------------------------------------------------------------


class TestNoStaleFunctionReferences:
    """Verify removed functions don't exist in pipelines.py."""

    def _get_function_names(self, filepath: Path) -> set[str]:
        """Extract all top-level and class function names from a Python file."""
        if not filepath.exists():
            return set()
        source = filepath.read_text()
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
        return names

    def test_no_tier3_functions_in_pipelines(self):
        pipelines_path = _project_root / "orchestrator" / "routes" / "pipelines.py"
        names = self._get_function_names(pipelines_path)
        removed_functions = {
            "_run_tier3_implement",
            "_run_single_phase_cycle",
            "_build_phase_scoped_prompt",
            "_run_multi_agent_phase",
            "_check_short_circuit_signal",
            "_check_high_complexity_signal",
        }
        found = names & removed_functions
        assert not found, f"Stale functions still in pipelines.py: {found}"


# ---------------------------------------------------------------------------
# Section 12: PhaseDependencyGraph removed
# ---------------------------------------------------------------------------


class TestPhaseDependencyGraphRemoval:
    """Verify PhaseDependencyGraph and PhaseWave are removed."""

    def test_no_phase_dependency_graph(self):
        from egg_contracts import dependency_graph

        assert not hasattr(dependency_graph, "PhaseDependencyGraph"), (
            "PhaseDependencyGraph should be removed from dependency_graph.py"
        )

    def test_no_phase_wave(self):
        from egg_contracts import dependency_graph

        assert not hasattr(dependency_graph, "PhaseWave"), (
            "PhaseWave should be removed from dependency_graph.py"
        )


# ---------------------------------------------------------------------------
# Section 13: Gateway agent_restrictions.py cleanup
# ---------------------------------------------------------------------------


class TestGatewayCleanup:
    """Verify gateway complexity tier references are removed."""

    def test_no_integrator_tier3_patterns_in_source(self):
        """agent_restrictions.py should not reference INTEGRATOR_TIER3_PATTERNS."""
        restrictions_path = _project_root / "gateway" / "agent_restrictions.py"
        if not restrictions_path.exists():
            pytest.skip("agent_restrictions.py not found")
        source = restrictions_path.read_text()
        assert "INTEGRATOR_TIER3_PATTERNS" not in source, (
            "INTEGRATOR_TIER3_PATTERNS should be removed from agent_restrictions.py"
        )

    def test_no_complexity_tier_in_gateway(self):
        """Gateway source files should not reference complexity_tier."""
        gateway_dir = _project_root / "gateway"
        if not gateway_dir.exists():
            pytest.skip("gateway directory not found")
        for pyfile in gateway_dir.glob("*.py"):
            source = pyfile.read_text()
            # Allow in test files (which are being updated) and __pycache__
            if "tests" in str(pyfile):
                continue
            assert "complexity_tier" not in source, (
                f"{pyfile.name} still references complexity_tier"
            )


# ---------------------------------------------------------------------------
# Section 14: Handoffs.py INTEGRATOR removed from ROLE_MAP
# ---------------------------------------------------------------------------


class TestHandoffsCleanup:
    """Verify handoffs.py removes INTEGRATOR from ROLE_MAP."""

    def test_no_integrator_in_handoffs_role_map(self):
        handoffs_path = _project_root / "orchestrator" / "handoffs.py"
        if not handoffs_path.exists():
            pytest.skip("handoffs.py not found")
        source = handoffs_path.read_text()
        assert "INTEGRATOR" not in source, "handoffs.py ROLE_MAP should not reference INTEGRATOR"


# ---------------------------------------------------------------------------
# Section 15: DAG visualizer tier3 rendering removed
# ---------------------------------------------------------------------------


class TestDagVisualizerCleanup:
    """Verify _render_tier3_implement is removed from dag_visualizer.py."""

    def test_no_render_tier3_in_dag_visualizer(self):
        viz_path = _project_root / "orchestrator" / "dag_visualizer.py"
        if not viz_path.exists():
            pytest.skip("dag_visualizer.py not found")
        source = viz_path.read_text()
        assert "_render_tier3_implement" not in source, (
            "_render_tier3_implement should be removed from dag_visualizer.py"
        )


# ---------------------------------------------------------------------------
# Section 16: Review graph has no integrator edges
# ---------------------------------------------------------------------------


class TestReviewGraphNoIntegrator:
    """Verify the implement review graph does not reference integrator."""

    def test_implement_graph_no_integrator_producer(self):
        from review_graph import get_default_implement_graph

        graph = get_default_implement_graph()
        for edge in graph.edges:
            assert edge.producer_role != "integrator", (
                f"Review graph still has integrator as producer: {edge}"
            )

    def test_implement_graph_no_integrator_reviewer(self):
        from review_graph import get_default_implement_graph

        graph = get_default_implement_graph()
        for edge in graph.edges:
            assert edge.reviewer_role != "integrator", (
                f"Review graph still has integrator as reviewer: {edge}"
            )

    def test_implement_graph_producers_are_expected(self):
        """After removal, only coder, tester, documenter are producers."""
        from review_graph import get_default_implement_graph

        graph = get_default_implement_graph()
        producers = {e.producer_role for e in graph.edges}
        # integrator must not be present
        assert "integrator" not in producers
        # Core producers must be present
        assert "coder" in producers
        assert "tester" in producers


# ---------------------------------------------------------------------------
# Section 17: Concurrent executor failure handling (edge cases)
# ---------------------------------------------------------------------------


class TestConcurrentExecutorFailureHandling:
    """Test failure handling in the concurrent executor — edge cases."""

    def test_single_failure_creates_hitl_decision(self):
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        mock_decision = MagicMock()
        mock_decision.id = "decision-1"

        with (
            patch("concurrent_executor.get_message_store") as mock_store,
            patch("concurrent_executor.get_peer_consensus_tracker") as mock_tracker,
            patch("concurrent_executor.emit_event"),
            patch.object(type(pipeline), "add_decision", return_value=mock_decision),
        ):
            mock_store.return_value = MagicMock()
            mock_tracker.return_value = MagicMock(
                handle_agent_crash=MagicMock(return_value={"action": "continue"})
            )

            result = executor.handle_agent_failure("coder", "container crash")
            assert result["action"] == "hitl_decision"
            assert result["failed_role"] == "coder"

    def test_multiple_failures_within_window_aborts_phase(self):
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        mock_decision = MagicMock()
        mock_decision.id = "decision-2"

        with (
            patch("concurrent_executor.get_message_store") as mock_store,
            patch("concurrent_executor.get_peer_consensus_tracker") as mock_tracker,
            patch("concurrent_executor.emit_event"),
            patch.object(type(pipeline), "add_decision", return_value=mock_decision),
        ):
            mock_store.return_value = MagicMock()
            mock_tracker.return_value = MagicMock(
                handle_agent_crash=MagicMock(return_value={"action": "continue"})
            )

            # First failure — handled as single
            result1 = executor.handle_agent_failure("coder", "crash 1")
            assert result1["action"] == "hitl_decision"

            # Second failure within window — triggers abort
            result2 = executor.handle_agent_failure("tester", "crash 2")
            assert result2["action"] == "phase_abort"
            assert result2["recent_failures"] >= 2

    def test_consensus_check_with_no_tracker_returns_incomplete(self):
        from concurrent_executor import ConcurrentPhaseExecutor

        pipeline = _make_pipeline()
        executor = ConcurrentPhaseExecutor(pipeline, spawn_fn=MagicMock())

        with patch("concurrent_executor.get_peer_consensus_tracker", return_value=None):
            result = executor.check_consensus()
            assert result["is_complete"] is False


# ---------------------------------------------------------------------------
# Section 18: Stale string references in Python source
# ---------------------------------------------------------------------------


class TestNoStaleStringReferences:
    """Grep key source files for stale references to removed concepts."""

    _STALE_PATTERNS = [
        "tier3",
        "tier_3",
        "_run_tier3",
        "short_circuit",
        "ComplexityTier",
        "INTEGRATOR_TIER3",
        "PhaseDependencyGraph",
        "PhaseWave",
        "_run_multi_agent_phase",
        "_build_phase_scoped_prompt",
    ]

    @pytest.mark.parametrize(
        "relative_path",
        [
            "orchestrator/routes/pipelines.py",
            "orchestrator/concurrent_executor.py",
            "orchestrator/routes/signals.py",
        ],
    )
    def test_no_stale_references_in_file(self, relative_path: str):
        filepath = _project_root / relative_path
        if not filepath.exists():
            pytest.skip(f"{relative_path} not found")
        source = filepath.read_text()
        for pattern in self._STALE_PATTERNS:
            assert pattern not in source, f"Stale reference '{pattern}' found in {relative_path}"
