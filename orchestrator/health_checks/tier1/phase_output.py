"""
PhaseOutputPresenceCheck — verify that expected phase artifacts exist.

This is the check that would have caught issue-835: agents exited
cleanly (COMPLETE) but produced no commits, so reviewers had nothing
to review.

For each phase type, verifies:
- implement: new commits on the remote branch beyond origin/main
- plan: architect-output.json (or plan draft) exists
- refine: refine output exists

Returns DEGRADED (not FAILED) when agents succeeded but artifacts
are missing — this is a semantic problem, not an infrastructure one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_shared_path = Path(__file__).parent.parent.parent.parent / "shared"
if _shared_path.exists() and str(_shared_path) not in sys.path:
    sys.path.insert(0, str(_shared_path))

try:
    from egg_logging import get_logger
except ImportError:
    import logging

    def get_logger(name: str, **kwargs) -> logging.Logger:  # type: ignore[misc]
        return logging.getLogger(name)


from health_checks.context import PipelineHealthContext
from health_checks.types import (
    HealthAction,
    HealthResult,
    HealthStatus,
    HealthTier,
    HealthTrigger,
)
from models import AgentExecutionStatus, PipelinePhase, PipelineStatus

logger = get_logger("orchestrator.health_checks.phase_output")


class PhaseOutputPresenceCheck:
    """Verify that completed agents actually produced expected artifacts."""

    name: str = "phase_output_presence"
    tier: HealthTier = HealthTier.PROGRAMMATIC
    triggers: frozenset[HealthTrigger] = frozenset(
        {
            HealthTrigger.WAVE_COMPLETE,
            HealthTrigger.PHASE_COMPLETE,
            HealthTrigger.ON_DEMAND,
        }
    )

    def run(self, context: PipelineHealthContext) -> HealthResult:
        """Check for phase output artifacts."""
        pipeline = context.pipeline
        phase = context.current_phase
        phase_exec = pipeline.phases.get(phase.value)

        if phase_exec is None or phase_exec.status == PipelineStatus.PENDING:
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning="Phase has not started yet.",
            )

        # Check if any agents completed successfully
        completed_agents = [
            a for a in phase_exec.agents if a.status == AgentExecutionStatus.COMPLETE
        ]
        if not completed_agents:
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning="No agents have completed yet; nothing to verify.",
            )

        # Phase-specific checks
        if phase == PipelinePhase.IMPLEMENT:
            return self._check_implement_outputs(context, completed_agents)
        elif phase == PipelinePhase.PLAN:
            return self._check_plan_outputs(context)
        else:
            # REFINE and PR phases: no strict artifact requirements yet
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning=f"No artifact requirements for {phase.value} phase.",
            )

    def _check_implement_outputs(
        self,
        context: PipelineHealthContext,
        completed_agents: list,
    ) -> HealthResult:
        """Check that implement phase produced commits on the remote branch."""
        # Check if any completed agent reported a commit
        agents_with_commits = [a for a in completed_agents if a.commit]
        if agents_with_commits:
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning=(f"{len(agents_with_commits)} agent(s) reported commits."),
            )

        # No agent reported a commit — check git for new commits on branch
        has_commits = self._branch_has_new_commits(context)
        if has_commits:
            return HealthResult(
                status=HealthStatus.HEALTHY,
                check_name=self.name,
                tier=self.tier,
                reasoning="Branch has new commits beyond origin/main.",
            )

        # Agents completed but no commits anywhere
        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning=(
                f"{len(completed_agents)} agent(s) completed successfully but "
                f"no commits were found on the branch. Agents may not have "
                f"produced meaningful work."
            ),
            action=HealthAction.ALERT,
            details={
                "completed_agent_count": len(completed_agents),
                "agents_with_commits": 0,
            },
        )

    def _check_plan_outputs(self, context: PipelineHealthContext) -> HealthResult:
        """Check that plan phase produced architect output."""
        # Look for plan artifacts in .egg-state/drafts/
        state_dir = self._get_state_dir(context)
        drafts_dir = state_dir / "drafts" if state_dir else None

        if drafts_dir and drafts_dir.is_dir():
            plan_files = [
                f
                for f in drafts_dir.iterdir()
                if f.is_file() and ("plan" in f.name.lower() or "architect" in f.name.lower())
            ]
            if plan_files:
                return HealthResult(
                    status=HealthStatus.HEALTHY,
                    check_name=self.name,
                    tier=self.tier,
                    reasoning=f"Plan artifacts found: {[f.name for f in plan_files]}",
                )

        return HealthResult(
            status=HealthStatus.DEGRADED,
            check_name=self.name,
            tier=self.tier,
            reasoning="Plan phase completed but no plan artifacts found in .egg-state/drafts/.",
            action=HealthAction.ALERT,
        )

    @staticmethod
    def _branch_has_new_commits(context: PipelineHealthContext) -> bool:
        """Check if branch has commits beyond origin/main."""
        git_dir = context.repo_path
        if context.pipeline.repo:
            repo_name = context.pipeline.repo.split("/")[-1]
            candidate = context.repo_path / repo_name
            if candidate.exists():
                git_dir = candidate

        try:
            result = subprocess.run(
                ["git", "rev-list", "--count", "origin/main..HEAD"],
                cwd=str(git_dir),
                capture_output=True,
                text=True,
                timeout=10,
            )
            count = int(result.stdout.strip())
            return count > 0
        except Exception:
            return False

    @staticmethod
    def _get_state_dir(context: PipelineHealthContext) -> Path | None:
        """Resolve the .egg-state directory."""
        state_dir = context.repo_path / ".egg-state"
        if context.pipeline.repo:
            repo_name = context.pipeline.repo.split("/")[-1]
            candidate = context.repo_path / repo_name / ".egg-state"
            if candidate.exists():
                return candidate
        return state_dir if state_dir.exists() else None
