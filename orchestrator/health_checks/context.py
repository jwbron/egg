"""
Pipeline health context with lazy properties (DD-7).

Expensive operations (git subprocess calls, file reads) only execute
when a property is first accessed.  Tier 1 checks typically only read
cheap attributes (pipeline, phase, containers); Tier 2 may access
git_log, agent_outputs, or contract which trigger subprocess / IO on
first use.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from models import Pipeline, PipelinePhase

# Approximate char limit for Tier 2 context fields (~4000 tokens × 4 chars/token).
_TIER2_CHAR_CAP = 16000


def _truncate(text: str, max_chars: int) -> str:
    """Truncate text to *max_chars*, appending an ellipsis marker if cut."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated]"


class PipelineHealthContext:
    """Read-only snapshot of pipeline state for health checks.

    All constructor parameters are cheap to provide.  Properties that
    require subprocess or file-system work are computed lazily and
    cached for the lifetime of this object.
    """

    def __init__(
        self,
        pipeline: Pipeline,
        repo_path: Path,
        trigger: str,
        phase: PipelinePhase | None = None,
        wave_number: int | None = None,
        docker_client: Any | None = None,
        state_store: Any | None = None,
    ) -> None:
        """Initialize context with cheap, already-loaded objects.

        Args:
            pipeline: The pipeline model (already loaded from state store).
            repo_path: Root path where repos are mounted (e.g. ~/repos).
            trigger: The HealthTrigger value that caused this check run.
            phase: Override for current phase (defaults to pipeline.current_phase).
            wave_number: Current wave number, if triggered by WAVE_COMPLETE.
            docker_client: DockerClient for live container queries. Optional —
                if None, live_container_ids returns an empty set.
            state_store: State store instance for additional lookups.
        """
        self.pipeline = pipeline
        self.repo_path = repo_path
        self.trigger = trigger
        self.phase = phase or pipeline.current_phase
        self.wave_number = wave_number
        self.docker_client = docker_client
        self.state_store = state_store
        self.timestamp = datetime.now(UTC)

        # Lazy caches — None means not yet computed
        self._git_log: str | None = None
        self._git_diff_stat: str | None = None
        self._agent_outputs: dict[str, str] | None = None
        self._contract: dict[str, object] | None = None
        self._live_container_ids: set[str] | None = None

    # ------------------------------------------------------------------
    # Cheap accessors (no IO)
    # ------------------------------------------------------------------

    @property
    def pipeline_id(self) -> str:
        """Pipeline identifier (e.g. ``issue-850``)."""
        return self.pipeline.id

    @property
    def branch(self) -> str | None:
        """Git branch associated with the pipeline, if any."""
        return self.pipeline.branch

    @property
    def current_phase(self) -> PipelinePhase:
        """The phase being evaluated (may be overridden from constructor)."""
        return self.phase

    # ------------------------------------------------------------------
    # Lazy properties (IO on first access)
    # ------------------------------------------------------------------

    @property
    def git_log(self) -> str:
        """Recent git log for the pipeline branch (last 20 commits)."""
        if self._git_log is None:
            self._git_log = self._run_git("log", "--oneline", "-20")
        return self._git_log

    @property
    def git_diff_stat(self) -> str:
        """Diff stat against origin/main, truncated to ~4000 tokens."""
        if self._git_diff_stat is None:
            raw = self._run_git("diff", "--stat", "origin/main...HEAD")
            self._git_diff_stat = _truncate(raw, _TIER2_CHAR_CAP)
        return self._git_diff_stat

    @property
    def agent_outputs(self) -> dict[str, str]:
        """Map of agent output filenames to their content.

        Reads from the .egg-state/ directory for the pipeline.
        """
        if self._agent_outputs is None:
            self._agent_outputs = self._read_agent_outputs()
        return self._agent_outputs

    @property
    def contract(self) -> dict[str, object]:
        """SDLC contract for the pipeline's issue (parsed JSON).

        Returns an empty dict if no contract exists or cannot be parsed.
        """
        if self._contract is None:
            self._contract = self._read_contract()
        return self._contract

    @property
    def live_container_ids(self) -> set[str]:
        """Set of currently-live Docker container IDs."""
        if self._live_container_ids is None:
            self._live_container_ids = self._fetch_live_container_ids()
        return self._live_container_ids

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_git(self, *args: str) -> str:
        """Run a git command in the repo directory, returning stdout."""
        # Determine the actual git working directory
        git_dir = self.repo_path
        if self.pipeline.repo:
            # repo is "owner/name" — the working copy is under repo_path/name
            repo_name = self.pipeline.repo.split("/")[-1]
            candidate = self.repo_path / repo_name
            if candidate.exists():
                git_dir = candidate

        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(git_dir),
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _resolve_state_dir(self) -> Path:
        """Return the .egg-state directory for this pipeline's repo."""
        state_dir = self.repo_path / ".egg-state"
        if self.pipeline.repo:
            repo_name = self.pipeline.repo.split("/")[-1]
            candidate = self.repo_path / repo_name / ".egg-state"
            if candidate.exists():
                state_dir = candidate
        return state_dir

    def _read_agent_outputs(self) -> dict[str, str]:
        """Read agent output files from .egg-state/."""
        outputs: dict[str, str] = {}
        state_dir = self._resolve_state_dir()

        if not state_dir.exists():
            return outputs

        # Read drafts, contracts, and agent-outputs
        for subdir_name in ("drafts", "contracts", "agent-outputs"):
            subdir = state_dir / subdir_name
            if subdir.is_dir():
                for path in subdir.iterdir():
                    if path.is_file() and path.suffix in (".json", ".md", ".yaml", ".yml"):
                        try:
                            outputs[path.name] = path.read_text(errors="replace")[:4000]
                        except Exception:
                            pass
        return outputs

    def _read_contract(self) -> dict[str, object]:
        """Read and parse the SDLC contract JSON for this pipeline's issue."""
        state_dir = self._resolve_state_dir()
        issue_number = self.pipeline.issue_number
        if issue_number is None:
            return {}

        contract_path = state_dir / "contracts" / f"{issue_number}.json"
        if not contract_path.is_file():
            return {}

        try:
            raw = contract_path.read_text(errors="replace")[:_TIER2_CHAR_CAP]
            return json.loads(raw)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, OSError):
            return {}

    def _fetch_live_container_ids(self) -> set[str]:
        """Query Docker for live container IDs."""
        if self.docker_client is None:
            return set()
        try:
            containers = self.docker_client.list_containers(all=False)
            return {c.container_id for c in containers}
        except Exception:
            return set()
