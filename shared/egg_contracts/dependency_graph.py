"""
Dependency graph resolver for multi-agent orchestration.

This module provides graph-based analysis of agent dependencies to determine
execution order and identify parallelizable groups (waves). The resolver
uses topological sorting to ensure agents run in the correct order while
maximizing parallelism where dependencies allow.

Key concepts:
- Wave: A group of agents that can run in parallel
- Dependency: An agent that must complete before another can start
- Cycle: A circular dependency (invalid configuration)
"""

from __future__ import annotations

import bisect
from collections import defaultdict, deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .agent_roles import AgentRole, get_role_definition

if TYPE_CHECKING:
    from .orchestration import OrchestrationState


@dataclass
class DependencyNode:
    """A node in the dependency graph."""

    role: AgentRole
    dependencies: list[AgentRole] = field(default_factory=list)
    dependents: list[AgentRole] = field(default_factory=list)

    def has_dependency(self, other: AgentRole) -> bool:
        """Check if this node depends on another."""
        return other in self.dependencies

    def add_dependency(self, other: AgentRole) -> None:
        """Add a dependency to this node."""
        if other not in self.dependencies:
            self.dependencies.append(other)

    def add_dependent(self, other: AgentRole) -> None:
        """Add a dependent to this node."""
        if other not in self.dependents:
            self.dependents.append(other)


@dataclass
class ExecutionWave:
    """A wave of agents that can execute in parallel.

    All agents in a wave have their dependencies satisfied when the
    wave starts, so they can run concurrently.
    """

    wave_number: int
    agents: list[AgentRole] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.agents)

    def __iter__(self) -> Iterator[AgentRole]:
        return iter(self.agents)

    def is_parallel(self) -> bool:
        """Check if this wave has multiple agents."""
        return len(self.agents) > 1


@dataclass
class ExecutionPlan:
    """Complete execution plan with ordered waves.

    The plan specifies the order in which agents should execute,
    grouped into waves that can run in parallel.
    """

    waves: list[ExecutionWave] = field(default_factory=list)
    total_agents: int = 0

    def add_wave(self, agents: list[AgentRole]) -> ExecutionWave:
        """Add a new wave to the plan."""
        wave = ExecutionWave(
            wave_number=len(self.waves) + 1,
            agents=agents,
        )
        self.waves.append(wave)
        self.total_agents += len(agents)
        return wave

    def get_wave(self, wave_number: int) -> ExecutionWave | None:
        """Get a specific wave by number (1-indexed)."""
        if 1 <= wave_number <= len(self.waves):
            return self.waves[wave_number - 1]
        return None

    def get_all_agents(self) -> list[AgentRole]:
        """Get all agents in execution order."""
        agents = []
        for wave in self.waves:
            agents.extend(wave.agents)
        return agents

    def __len__(self) -> int:
        return len(self.waves)

    def __iter__(self) -> Iterator[ExecutionWave]:
        return iter(self.waves)


class DependencyGraph:
    """Graph representation of agent dependencies.

    Provides methods for analyzing dependencies and computing
    execution order.
    """

    def __init__(self) -> None:
        self.nodes: dict[AgentRole, DependencyNode] = {}
        self._built = False

    def add_node(self, role: AgentRole) -> DependencyNode:
        """Add a node to the graph."""
        if role not in self.nodes:
            self.nodes[role] = DependencyNode(role=role)
        return self.nodes[role]

    def add_edge(self, from_role: AgentRole, to_role: AgentRole) -> None:
        """Add a dependency edge (from depends on to)."""
        from_node = self.add_node(from_role)
        to_node = self.add_node(to_role)

        from_node.add_dependency(to_role)
        to_node.add_dependent(from_role)

    def build_from_roles(self, roles: list[AgentRole] | None = None) -> None:
        """Build the graph from agent role definitions.

        Args:
            roles: Specific roles to include (None = all roles)
        """
        if roles is None:
            roles = list(AgentRole)

        # Add all nodes first
        for role in roles:
            self.add_node(role)

        # Add edges based on role definitions
        for role in roles:
            role_def = get_role_definition(role)
            for dep in role_def.dependencies:
                if dep in roles:
                    self.add_edge(role, dep)

        self._built = True

    def has_cycle(self) -> bool:
        """Check if the graph has any cycles.

        Uses DFS to detect cycles, which would indicate an invalid
        configuration (circular dependencies).
        """
        visited = set()
        rec_stack = set()

        def dfs(role: AgentRole) -> bool:
            visited.add(role)
            rec_stack.add(role)

            node = self.nodes[role]
            for dep in node.dependencies:
                if dep not in visited:
                    if dfs(dep):
                        return True
                elif dep in rec_stack:
                    return True

            rec_stack.remove(role)
            return False

        for role in self.nodes:
            if role not in visited:
                if dfs(role):
                    return True

        return False

    def topological_sort(self) -> list[AgentRole]:
        """Return agents in topological order.

        Dependencies appear before the agents that depend on them.
        Raises ValueError if the graph has cycles.
        """
        if self.has_cycle():
            raise ValueError("Dependency graph has cycles")

        in_degree: dict[AgentRole, int] = defaultdict(int)

        # Calculate in-degree for each node
        for node in self.nodes.values():
            for _dep in node.dependencies:
                in_degree[node.role] += 1

        # Start with nodes that have no dependencies
        queue = deque(role for role in self.nodes if in_degree[role] == 0)
        result = []

        while queue:
            role = queue.popleft()
            result.append(role)

            node = self.nodes[role]
            for dependent in node.dependents:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(result) != len(self.nodes):
            raise ValueError("Could not process all nodes - cycle detected")

        return result

    def compute_waves(self) -> list[list[AgentRole]]:
        """Compute execution waves for parallel execution.

        Returns a list of waves, where each wave contains agents that
        can run in parallel (all their dependencies are in earlier waves).
        """
        if self.has_cycle():
            raise ValueError("Dependency graph has cycles")

        # Track which wave each role is assigned to
        role_wave: dict[AgentRole, int] = {}
        waves: list[list[AgentRole]] = []

        # Process roles in topological order
        sorted_roles = self.topological_sort()

        for role in sorted_roles:
            node = self.nodes[role]

            # Find the wave this role can join (after all dependencies)
            max_dep_wave = -1
            for dep in node.dependencies:
                if dep in role_wave:
                    max_dep_wave = max(max_dep_wave, role_wave[dep])

            # Assign to the next wave after the latest dependency
            assigned_wave = max_dep_wave + 1
            role_wave[role] = assigned_wave

            # Ensure wave list is long enough
            while len(waves) <= assigned_wave:
                waves.append([])

            waves[assigned_wave].append(role)

        return waves

    def get_execution_plan(self) -> ExecutionPlan:
        """Compute and return a complete execution plan.

        Returns:
            ExecutionPlan with ordered waves
        """
        waves = self.compute_waves()
        plan = ExecutionPlan()

        for agents in waves:
            if agents:
                plan.add_wave(agents)

        return plan


def build_dependency_graph(roles: list[AgentRole] | None = None) -> DependencyGraph:
    """Build a dependency graph for the given roles.

    Args:
        roles: Specific roles to include (None = all roles)

    Returns:
        Configured DependencyGraph
    """
    graph = DependencyGraph()
    graph.build_from_roles(roles)
    return graph


def compute_execution_plan(roles: list[AgentRole] | None = None) -> ExecutionPlan:
    """Compute the execution plan for the given roles.

    Args:
        roles: Specific roles to include (None = all roles)

    Returns:
        ExecutionPlan with ordered waves
    """
    graph = build_dependency_graph(roles)
    return graph.get_execution_plan()


def get_parallel_groups(
    state: OrchestrationState,
) -> list[list[AgentRole]]:
    """Get groups of agents that can run in parallel based on current state.

    This considers the current execution state to determine which pending
    agents can run together.

    Args:
        state: Current orchestration state

    Returns:
        List of parallel groups (each group can run concurrently)
    """
    from .orchestration import can_agent_run

    # Get all pending roles that can run (only from registered executions)
    runnable = []
    for role in state.executions:
        if can_agent_run(role, state):
            runnable.append(role)

    if not runnable:
        return []

    # All runnable agents can run in parallel since their dependencies
    # are already satisfied
    return [runnable]


def format_execution_plan(plan: ExecutionPlan) -> str:
    """Format an execution plan as a human-readable string.

    Args:
        plan: The execution plan to format

    Returns:
        Formatted string representation
    """
    lines = [f"Execution Plan ({plan.total_agents} agents, {len(plan)} waves):"]

    for wave in plan:
        parallel_marker = " [parallel]" if wave.is_parallel() else ""
        agents_str = ", ".join(r.value for r in wave.agents)
        lines.append(f"  Wave {wave.wave_number}{parallel_marker}: {agents_str}")

    return "\n".join(lines)


# --- Phase-level dependency graph for Tier 3 ---


@dataclass
class PhaseWave:
    """A wave of plan phases that can execute in parallel.

    All phases in a wave have their phase-level dependencies satisfied,
    so they can be implemented concurrently.
    """

    wave_number: int
    phase_ids: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.phase_ids)

    def is_parallel(self) -> bool:
        """Check if this wave has multiple phases."""
        return len(self.phase_ids) > 1


class PhaseDependencyGraph:
    """Dependency graph for plan phases (Tier 3 dispatch).

    Computes execution waves for plan phases based on their declared
    dependencies. Independent phases are grouped into the same wave
    for parallel execution.

    Example:
        phases = [
            Phase(id="phase-1", dependencies=[]),
            Phase(id="phase-2", dependencies=["phase-1"]),
            Phase(id="phase-3", dependencies=["phase-1"]),
            Phase(id="phase-4", dependencies=["phase-2", "phase-3"]),
        ]
        graph = PhaseDependencyGraph(phases)
        waves = graph.compute_waves()
        # waves[0] = PhaseWave(1, ["phase-1"])
        # waves[1] = PhaseWave(2, ["phase-2", "phase-3"])  # parallel
        # waves[2] = PhaseWave(3, ["phase-4"])
    """

    def __init__(self, phases: list | None = None) -> None:
        """Initialize with optional list of Phase models.

        Args:
            phases: List of Phase models (from contract). Each phase must have
                an `id` (str) and `dependencies` (list[str]) attribute.
        """
        self.nodes: dict[str, list[str]] = {}  # phase_id -> list of dependency phase_ids
        if phases:
            for phase in phases:
                deps = getattr(phase, "dependencies", []) or []
                self.nodes[phase.id] = list(deps)

    def add_phase(self, phase_id: str, dependencies: list[str] | None = None) -> None:
        """Add a phase to the graph.

        Args:
            phase_id: The phase identifier (e.g., 'phase-1')
            dependencies: Phase IDs this phase depends on
        """
        self.nodes[phase_id] = list(dependencies or [])

    def has_cycle(self) -> bool:
        """Check if the graph has any cycles.

        Returns:
            True if a cycle is detected
        """
        visited: set[str] = set()
        rec_stack: set[str] = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)

            for dep in self.nodes.get(node, []):
                if dep not in self.nodes:
                    continue  # Skip unknown dependencies
                if dep not in visited:
                    if dfs(dep):
                        return True
                elif dep in rec_stack:
                    return True

            rec_stack.discard(node)
            return False

        for node in self.nodes:
            if node not in visited:
                if dfs(node):
                    return True

        return False

    def topological_sort(self) -> list[str]:
        """Return phase IDs in topological order.

        Raises:
            ValueError: If the graph has cycles
        """
        if self.has_cycle():
            raise ValueError("Phase dependency graph has cycles")

        in_degree: dict[str, int] = dict.fromkeys(self.nodes, 0)
        for pid, deps in self.nodes.items():
            for dep in deps:
                if dep in self.nodes:
                    in_degree[pid] += 1

        queue = sorted(pid for pid in self.nodes if in_degree[pid] == 0)
        result: list[str] = []

        while queue:
            pid = queue.pop(0)
            result.append(pid)

            # Find all nodes that depend on pid
            for other_pid, deps in self.nodes.items():
                if pid in deps:
                    in_degree[other_pid] -= 1
                    if in_degree[other_pid] == 0:
                        # Insert sorted to get deterministic ordering
                        bisect.insort(queue, other_pid)

        if len(result) != len(self.nodes):
            raise ValueError("Could not process all phases - cycle detected")

        return result

    def compute_waves(self) -> list[PhaseWave]:
        """Compute execution waves for parallel phase execution.

        Returns a list of PhaseWave objects, where each wave contains
        phases that can run concurrently.

        Raises:
            ValueError: If the graph has cycles
        """
        if not self.nodes:
            return []

        sorted_phases = self.topological_sort()

        # Track which wave each phase is assigned to
        phase_wave: dict[str, int] = {}
        waves: list[list[str]] = []

        for pid in sorted_phases:
            deps = self.nodes.get(pid, [])

            # Find the wave this phase can join (after all dependencies)
            max_dep_wave = -1
            for dep in deps:
                if dep in phase_wave:
                    max_dep_wave = max(max_dep_wave, phase_wave[dep])

            assigned_wave = max_dep_wave + 1
            phase_wave[pid] = assigned_wave

            while len(waves) <= assigned_wave:
                waves.append([])
            waves[assigned_wave].append(pid)

        return [
            PhaseWave(wave_number=i + 1, phase_ids=phase_ids)
            for i, phase_ids in enumerate(waves)
            if phase_ids
        ]

    def get_sequential_order(self) -> list[str]:
        """Get phases in sequential execution order.

        Returns phases in topological order, suitable for sequential
        (non-parallel) Tier 3 execution.
        """
        return self.topological_sort()
