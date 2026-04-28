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

from collections import defaultdict, deque
from collections.abc import Hashable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .agent_roles import AgentRole, get_role_definition

if TYPE_CHECKING:
    from .orchestration import OrchestrationState

# ---------------------------------------------------------------------------
# #2137 — generification: the dependency-graph machinery is reused for both
# the agent-role DAG (the original use case, AgentRole-keyed) and the slice
# DAG used by the new SliceScheduler (str-keyed slice IDs). PEP-695 generic
# class syntax (Python 3.13 target) lets both keying strategies share a
# single implementation. Concrete callers parameterise the type via
# ``DependencyGraph[AgentRole]`` / ``DependencyGraph[str]``.
# ---------------------------------------------------------------------------


@dataclass
class DependencyNode[NodeT: Hashable]:
    """A node in a generic dependency graph.

    Generified in #2137 (TASK-3-1) — was ``AgentRole``-keyed only. The
    ``role`` attribute is preserved (it stores the node's identity)
    but is now of type ``NodeT``; callers that key by ``AgentRole``
    continue to work via ``DependencyGraph[AgentRole]``, while the
    new slice scheduler uses ``DependencyGraph[str]``.
    """

    role: NodeT
    dependencies: list[NodeT] = field(default_factory=list)
    dependents: list[NodeT] = field(default_factory=list)

    def has_dependency(self, other: NodeT) -> bool:
        """Check if this node depends on another."""
        return other in self.dependencies

    def add_dependency(self, other: NodeT) -> None:
        """Add a dependency to this node."""
        if other not in self.dependencies:
            self.dependencies.append(other)

    def add_dependent(self, other: NodeT) -> None:
        """Add a dependent to this node."""
        if other not in self.dependents:
            self.dependents.append(other)


@dataclass
class ExecutionWave[NodeT: Hashable]:
    """A wave of nodes that can execute in parallel.

    All nodes in a wave have their dependencies satisfied when the
    wave starts, so they can run concurrently. The original
    role-keyed API continues to work as ``ExecutionWave[AgentRole]``;
    the slice scheduler uses ``ExecutionWave[str]``.
    """

    wave_number: int
    agents: list[NodeT] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.agents)

    def __iter__(self) -> Iterator[NodeT]:
        return iter(self.agents)

    def is_parallel(self) -> bool:
        """Check if this wave has multiple nodes."""
        return len(self.agents) > 1


@dataclass
class ExecutionPlan[NodeT: Hashable]:
    """Complete execution plan with ordered waves.

    The plan specifies the order in which nodes should execute,
    grouped into waves that can run in parallel. Generified in
    #2137 (TASK-3-1).
    """

    waves: list[ExecutionWave[NodeT]] = field(default_factory=list)
    total_agents: int = 0

    def add_wave(self, agents: list[NodeT]) -> ExecutionWave[NodeT]:
        """Add a new wave to the plan."""
        wave: ExecutionWave[NodeT] = ExecutionWave(
            wave_number=len(self.waves) + 1,
            agents=agents,
        )
        self.waves.append(wave)
        self.total_agents += len(agents)
        return wave

    def get_wave(self, wave_number: int) -> ExecutionWave[NodeT] | None:
        """Get a specific wave by number (1-indexed)."""
        if 1 <= wave_number <= len(self.waves):
            return self.waves[wave_number - 1]
        return None

    def get_all_agents(self) -> list[NodeT]:
        """Get all nodes in execution order."""
        agents: list[NodeT] = []
        for wave in self.waves:
            agents.extend(wave.agents)
        return agents

    def __len__(self) -> int:
        return len(self.waves)

    def __iter__(self) -> Iterator[ExecutionWave[NodeT]]:
        return iter(self.waves)


class DependencyGraph[NodeT: Hashable]:
    """Graph representation of node dependencies.

    Provides methods for analyzing dependencies and computing
    execution order. Generified in #2137 (TASK-3-1) so the same
    machinery powers both the agent-role DAG (``DependencyGraph[
    AgentRole]``) and the slice DAG (``DependencyGraph[str]``).
    """

    def __init__(self) -> None:
        self.nodes: dict[NodeT, DependencyNode[NodeT]] = {}
        self._built = False

    def add_node(self, role: NodeT) -> DependencyNode[NodeT]:
        """Add a node to the graph."""
        if role not in self.nodes:
            self.nodes[role] = DependencyNode(role=role)
        return self.nodes[role]

    def add_edge(self, from_role: NodeT, to_role: NodeT) -> None:
        """Add a dependency edge (from depends on to)."""
        from_node = self.add_node(from_role)
        to_node = self.add_node(to_role)

        from_node.add_dependency(to_role)
        to_node.add_dependent(from_role)

    def build_from_roles(self, roles: list[AgentRole] | None = None) -> None:
        """Build the graph from agent role definitions.

        Only meaningful when the graph is keyed on ``AgentRole``
        (i.e. ``DependencyGraph[AgentRole]``); the slice-DAG flavour
        ``DependencyGraph[str]`` populates itself directly via
        ``add_node`` / ``add_edge`` from ``Contract.slices``.

        Args:
            roles: Specific roles to include (None = all roles)
        """
        if roles is None:
            from .agent_roles import AGENT_ROLES

            roles = [r for r in AgentRole if r in AGENT_ROLES]

        # AgentRole leaks into this method body even on
        # DependencyGraph[NodeT] for arbitrary NodeT — that's
        # intentional: ``build_from_roles`` is the
        # AgentRole-specific helper. The casts below tell mypy that
        # AgentRole IS the NodeT bound for any caller that would
        # actually invoke this method (other callers go through
        # add_node/add_edge directly).
        from typing import cast as _cast

        # Add all nodes first
        for role in roles:
            self.add_node(_cast(NodeT, role))

        # Add edges based on role definitions
        for role in roles:
            role_def = get_role_definition(role)
            for dep in role_def.dependencies:
                if dep in roles:
                    self.add_edge(_cast(NodeT, role), _cast(NodeT, dep))

        self._built = True

    def has_cycle(self) -> bool:
        """Check if the graph has any cycles.

        Uses DFS to detect cycles, which would indicate an invalid
        configuration (circular dependencies).
        """
        visited: set[NodeT] = set()
        rec_stack: set[NodeT] = set()

        def dfs(role: NodeT) -> bool:
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

    def topological_sort(self) -> list[NodeT]:
        """Return nodes in topological order.

        Dependencies appear before the nodes that depend on them.
        Raises ValueError if the graph has cycles.
        """
        if self.has_cycle():
            raise ValueError("Dependency graph has cycles")

        in_degree: dict[NodeT, int] = defaultdict(int)

        # Calculate in-degree for each node
        for node in self.nodes.values():
            for _dep in node.dependencies:
                in_degree[node.role] += 1

        # Start with nodes that have no dependencies
        queue: deque[NodeT] = deque(role for role in self.nodes if in_degree[role] == 0)
        result: list[NodeT] = []

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

    def compute_waves(self) -> list[list[NodeT]]:
        """Compute execution waves for parallel execution.

        Returns a list of waves, where each wave contains nodes that
        can run in parallel (all their dependencies are in earlier waves).
        """
        if self.has_cycle():
            raise ValueError("Dependency graph has cycles")

        # Track which wave each node is assigned to
        role_wave: dict[NodeT, int] = {}
        waves: list[list[NodeT]] = []

        # Process nodes in topological order
        sorted_roles = self.topological_sort()

        for role in sorted_roles:
            node = self.nodes[role]

            # Find the wave this node can join (after all dependencies)
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

    def get_execution_plan(self) -> ExecutionPlan[NodeT]:
        """Compute and return a complete execution plan.

        Returns:
            ExecutionPlan with ordered waves
        """
        waves = self.compute_waves()
        plan: ExecutionPlan[NodeT] = ExecutionPlan()

        for agents in waves:
            if agents:
                plan.add_wave(agents)

        return plan


def build_dependency_graph(
    roles: list[AgentRole] | None = None,
) -> DependencyGraph[AgentRole]:
    """Build an AgentRole-keyed dependency graph for the given roles.

    Args:
        roles: Specific roles to include (None = all roles)

    Returns:
        Configured ``DependencyGraph[AgentRole]``
    """
    graph: DependencyGraph[AgentRole] = DependencyGraph()
    graph.build_from_roles(roles)
    return graph


def compute_execution_plan(
    roles: list[AgentRole] | None = None,
) -> ExecutionPlan[AgentRole]:
    """Compute the execution plan for the given roles.

    Args:
        roles: Specific roles to include (None = all roles)

    Returns:
        ``ExecutionPlan[AgentRole]`` with ordered waves
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


def format_execution_plan(plan: ExecutionPlan[AgentRole]) -> str:
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
